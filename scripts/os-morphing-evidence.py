import base64
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import grp
import subprocess


def command(arguments):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=30)
    return {'exit_code': result.returncode, 'output': result.stdout.strip()}


def key_fingerprints(text):
    fingerprints = set()
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            continue
        fields = line.split()
        for index, field in enumerate(fields[:-1]):
            if field.startswith(('ssh-', 'ecdsa-', 'sk-')):
                try:
                    digest = hashlib.sha256(base64.b64decode(fields[index + 1], validate=True)).digest()
                    fingerprints.add('SHA256:' + base64.b64encode(digest).decode().rstrip('='))
                except ValueError:
                    pass
                break
    return sorted(fingerprints)


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def driver(path):
    for directory in (path.resolve(), *path.resolve().parents):
        binding = directory / 'driver'
        if binding.is_symlink():
            return binding.resolve().name
    return None


def service(name):
    result = command(['systemctl', 'show', name, '--no-pager',
                      '--property=LoadState,ActiveState,SubState,UnitFileState,Result'])
    return dict(line.split('=', 1) for line in result['output'].splitlines() if '=' in line)


def accounts():
    result = []
    for account in pwd.getpwall():
        keys = Path(account.pw_dir) / '.ssh/authorized_keys'
        password = command(['passwd', '-S', account.pw_name])['output'].split()
        result.append({
            'user': account.pw_name, 'uid': account.pw_uid, 'gid': account.pw_gid,
            'home': account.pw_dir, 'shell': account.pw_shell,
            'groups': sorted(grp.getgrgid(group).gr_name for group in
                             os.getgrouplist(account.pw_name, account.pw_gid)),
            'password_status': password[1] if len(password) > 1 else 'unknown',
            'authorized_key_fingerprints': key_fingerprints(keys.read_text()) if keys.is_file() else [],
        })
    return sorted(result, key=lambda account: account['user'])


def collect():
    packages = command(['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Status}\t${Version}\n'])
    package_rows = [line.split('\t') for line in packages['output'].splitlines()]
    relevant_packages = [row for row in package_rows if len(row) == 3 and
                         any(term in row[0] for term in
                             ('vmware', 'open-vm-tools', 'stackit', 'cloud-init', 'qemu-guest'))]
    ssh_address = str(ipaddress.ip_address(os.environ.get('SCF_SSH_CLIENT_ADDRESS', '192.0.2.1')))
    ssh = command(['/usr/sbin/sshd', '-T', '-C',
                   'user=ubuntu,host=scf-relocate-app,addr=' + ssh_address])
    ssh_keys = {'passwordauthentication', 'pubkeyauthentication', 'permitrootlogin',
                'permitemptypasswords', 'authenticationmethods', 'authorizedkeysfile', 'usepam'}
    ssh_policy = dict(line.split(' ', 1) for line in ssh['output'].splitlines()
                      if line.split(' ', 1)[0] in ssh_keys)
    host_keys = []
    for key_path in sorted(Path('/etc/ssh').glob('ssh_host_*_key.pub')):
        host_keys.extend(key_fingerprints(key_path.read_text()))
    policies = [Path('/etc/sudoers'), *sorted(Path('/etc/sudoers.d').glob('*'))]
    unit_links = {}
    for directory in ('/etc/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system'):
        path = Path(directory) / 'vmtoolsd.service'
        if path.is_symlink():
            unit_links[str(path)] = {'target': os.readlink(path), 'target_exists': path.exists()}
    failed = command(['systemctl', 'list-units', '--failed', '--plain', '--no-legend', '--no-pager'])
    modules = Path('/proc/modules').read_text().splitlines()
    mounts = command(['findmnt', '--json', '--output', 'TARGET,SOURCE,FSTYPE,UUID,OPTIONS'])
    return {
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'kernel': command(['uname', '-r'])['output'],
        'os_release': dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                           if '=' in line),
        'hypervisor': command(['systemd-detect-virt'])['output'],
        'efi_boot': Path('/sys/firmware/efi').is_dir(),
        'machine_id_sha256': file_digest(Path('/etc/machine-id')),
        'packages': relevant_packages,
        'services': {name: service(name) for name in (
            'open-vm-tools.service', 'vmtoolsd.service', 'stackit-server-agent.service',
            'stackit-server-monitoring-agent.service', 'cloud-init.service',
            'ssh.service', 'relocate-demo.service', 'postgresql.service',
            'postgresql@16-relocate.service')},
        'vmtools_unit_links': unit_links,
        'failed_units': [line.split()[0] for line in failed['output'].splitlines() if line.split()],
        'relevant_loaded_modules': sorted(line.split()[0] for line in modules
                                         if line.startswith(('virtio', 'vmw', 'vmx', 'pvscsi'))),
        'pci_drivers': {path.name: driver(path) for path in sorted(Path('/sys/bus/pci/devices').glob('*'))},
        'block_drivers': {path.name: driver(path / 'device') for path in sorted(Path('/sys/class/block').glob('*'))
                          if (path / 'device').exists()},
        'network_drivers': {path.name: driver(path / 'device') for path in sorted(Path('/sys/class/net').glob('*'))
                            if (path / 'device').exists()},
        'block_devices': json.loads(command(['lsblk', '--json', '--output',
                                             'NAME,TYPE,SIZE,FSTYPE,UUID,MOUNTPOINTS'])['output']),
        'mounts': json.loads(mounts['output']),
        'fstab': [line for line in Path('/etc/fstab').read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith('#')],
        'ipv4_addresses': json.loads(command(['ip', '-json', '-4', 'address'])['output']),
        'ipv4_routes': json.loads(command(['ip', '-json', '-4', 'route'])['output']),
        'dns': command(['resolvectl', 'dns']),
        'cloud_init': command(['cloud-init', 'status', '--format', 'json']),
        'accounts': accounts(), 'ssh_policy': ssh_policy,
        'ssh_policy_client_address': ssh_address,
        'ssh_policy_exit_code': ssh['exit_code'],
        'ssh_host_key_fingerprints': sorted(host_keys),
        'sudo_policy_sha256': {str(path): file_digest(path) for path in policies if path.is_file()},
    }


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo for complete read-only account and SSH evidence.')
    print(json.dumps(collect(), indent=2))