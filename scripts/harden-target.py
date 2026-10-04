#!/usr/bin/env python3
"""Apply scoped post-morphing SSH policy and stale VMware alias cleanup."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid


def verify_target(server_id):
    expected = str(uuid.UUID(server_id))
    if subprocess.check_output(['systemd-detect-virt'], text=True).strip() != 'kvm':
        raise RuntimeError('Target hardening requires the approved KVM destination')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://169.254.169.254/openstack/latest/meta_data.json', timeout=5) as response:
        metadata = json.load(response)
    if metadata.get('uuid') != expected:
        raise RuntimeError('Target metadata does not match the explicitly approved server')
    return expected


def policy(source):
    address = str(ipaddress.IPv4Address(source))
    result = subprocess.run([
        '/usr/sbin/sshd', '-T', '-C', f'user=ubuntu,host=scf-relocate-app,addr={address}'],
        text=True, capture_output=True, check=False)
    if result.returncode:
        if result.stderr.strip() == 'Missing privilege separation directory: /run/sshd':
            return {'runtime': 'socket-activated SSH service has not created /run/sshd'}
        raise RuntimeError('SSH policy inspection failed; refusing hardening')
    return dict(line.split(' ', 1) for line in result.stdout.splitlines()
                if line.startswith(('passwordauthentication ', 'pubkeyauthentication ')))


def prepare_ssh_runtime():
    result = subprocess.run(['/usr/sbin/sshd', '-t'], text=True, capture_output=True, check=False)
    if result.returncode:
        if result.stderr.strip() != 'Missing privilege separation directory: /run/sshd':
            raise RuntimeError('SSH configuration failed before runtime preparation')
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'start', 'ssh.service'], check=True)
        subprocess.run(['/usr/sbin/sshd', '-t'], check=True)


def stale_alias(path):
    if not path.is_symlink():
        if path.exists():
            raise RuntimeError('Unexpected vmtoolsd unit file; refusing cleanup')
        return None
    target = os.readlink(path)
    if target not in ('/usr/lib/systemd/system/open-vm-tools.service',
                      '/lib/systemd/system/open-vm-tools.service') or path.exists():
        raise RuntimeError('Only the proven dangling VMware Tools alias may be removed')
    return target


def owned_file(path, content):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text() != content)):
        raise RuntimeError('Existing policy file differs; refusing overwrite: ' + str(path))


def apply_policies(files, alias, source):
    alias_target = stale_alias(alias)
    for path, content in files.items():
        owned_file(path, content)
    subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
    created = []
    created_directories = []
    removed_alias = False
    try:
        for path, content in files.items():
            if not path.exists():
                if not path.parent.exists():
                    path.parent.mkdir(mode=0o755)
                    created_directories.append(path.parent)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                created.append(path)
                with os.fdopen(descriptor, 'w') as stream:
                    stream.write(content)
        subprocess.run(['/usr/sbin/sshd', '-t'], check=True)
        if policy(source) != {'pubkeyauthentication': 'yes', 'passwordauthentication': 'no'}:
            raise RuntimeError('Effective SSH policy is not key-only; refusing reload')
        if alias_target:
            alias.unlink()
            removed_alias = True
        if removed_alias or any(path.suffix == '.conf' and 'systemd' in path.parts for path in created):
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'reload', 'ssh.service'], check=True)
    except Exception:
        for path in reversed(created):
            path.unlink()
        for directory in reversed(created_directories):
            directory.rmdir()
        if removed_alias:
            alias.symlink_to(alias_target)
            subprocess.run(['systemctl', 'daemon-reload'], check=False)
        subprocess.run(['systemctl', 'reload', 'ssh.service'], check=False)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-server-id', required=True)
    parser.add_argument('--ssh-source', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with approved passwordless sudo on the exact destination')
    server_id = verify_target(args.expected_server_id)
    files = {
        Path('/etc/ssh/sshd_config.d/00-scf-relocate-ssh.conf'): 'PasswordAuthentication no\n',
        Path('/etc/cloud/cloud.cfg.d/99-scf-relocate-ssh.cfg'): 'ssh_pwauth: false\n',
        Path('/etc/systemd/system/relocate-demo.service.d/10-scf-sigterm.conf'):
            '[Service]\nSuccessExitStatus=143\n',
    }
    alias = Path('/etc/systemd/system/vmtoolsd.service')
    alias_target = stale_alias(alias)
    for path, content in files.items():
        owned_file(path, content)
    if args.apply:
        prepare_ssh_runtime()
    result = {'server_id': server_id, 'apply': args.apply, 'before': policy(args.ssh_source),
              'policy_files': [str(path) for path in files], 'remove_stale_alias': bool(alias_target),
              'machine_id_policy': 'preserved; isolate parallel rehearsal identity consumers'}
    if args.apply:
        apply_policies(files, alias, args.ssh_source)
        result.update(after=policy(args.ssh_source), result='PASS')
    else:
        result['result'] = 'VALIDATED_PLAN'
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()