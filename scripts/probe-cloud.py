#!/usr/bin/env python3
import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
MANIFEST = LOCAL / 'probe-resources.json'


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['provision', 'status', 'cleanup', 'inventory'])
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--region', default='eu01')
    parser.add_argument('--zone', default='eu01-1')
    parser.add_argument('--machine-type', default='c3i.2')
    parser.add_argument('--image-id')
    parser.add_argument('--state-dir', type=Path, default=LOCAL)
    parser.add_argument('--user-data-file', type=Path)
    parser.add_argument('--boot-volume-size', type=int, default=16)
    parser.add_argument('--ssh-source')
    parser.add_argument('--credentials', default='~/.ssh/cf-migration-sa.json')
    parser.add_argument('--cli', default=str(LOCAL / 'tools/stackit'))
    args = parser.parse_args()
    local = args.state_dir.resolve()
    if not local.is_relative_to(LOCAL.resolve()):
        parser.error('--state-dir must be inside the ignored .local directory')
    if args.ssh_source:
        source = ipaddress.ip_network(args.ssh_source, strict=True)
        if source.version != 4 or source.prefixlen != 32 or not source.is_global:
            parser.error('--ssh-source must be one public IPv4 address with /32')
        if not args.user_data_file:
            parser.error('--ssh-source requires explicit key-only cloud-init via --user-data-file')
    if args.boot_volume_size < 16:
        parser.error('--boot-volume-size must be at least 16 GiB')
    os.umask(0o077)
    local.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = local / 'probe-resources.json'
    auth = subprocess.run(
        [args.cli, 'auth', 'activate-service-account', '--service-account-key-path',
         str(Path(args.credentials).expanduser()), '--only-print-access-token'],
        capture_output=True, text=True, timeout=90,
    )
    if auth.returncode:
        raise RuntimeError('Service-account authentication failed; no credentials logged')
    environment = dict(os.environ, STACKIT_ACCESS_TOKEN=auth.stdout.strip())

    def cloud(*command):
        print('STACKIT ' + ' '.join(command[:3]), flush=True)
        result = subprocess.run(
            [args.cli, *command, '--project-id', args.project_id, '--region', args.region,
             '--output-format', 'json', '--assume-yes'],
            env=environment, capture_output=True, text=True, timeout=600,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout) if result.stdout.strip() else None

    if args.action == 'inventory':
        for kind in ['server', 'volume', 'network', 'network-interface', 'public-ip', 'security-group']:
            result = cloud(kind, 'list')
            save(local / (kind + '-inventory.json'), result)
            print(json.dumps(result, indent=2))
        return

    state = json.loads(manifest.read_text()) if manifest.exists() else {
        'project_id': args.project_id, 'region': args.region, 'resources': {},
        'name': 'scf-nested-probe-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S'),
    }
    if (state['project_id'], state['region']) != (args.project_id, args.region):
        raise RuntimeError('Manifest project or region does not match; refusing changes')
    resources = state['resources']

    def create(kind, *command):
        state['pending_creation'] = kind
        save(manifest, state)
        result = cloud(*command)
        save(local / (kind + '-created.json'), result)
        resources[kind] = result['id']
        state.pop('pending_creation')
        save(manifest, state)
        return result['id']

    if args.action == 'provision':
        if resources or state.get('pending_creation'):
            raise RuntimeError('Existing or uncertain probe resources: inspect status/inventory before retry')
        if not args.image_id:
            parser.error('--image-id is required for provision')
        state.pop('cleanup_commands_completed', None)
        state.update(machine_type=args.machine_type, zone=args.zone, image_id=args.image_id,
                 boot_volume_size=args.boot_volume_size, ssh_source=args.ssh_source)
        save(manifest, state)
        network = create('network', 'network', 'create', '--name', state['name'],
                         '--ipv4-prefix', '10.77.240.0/24', '--labels', 'purpose=nested-virt-probe')
        group = create('security-group', 'security-group', 'create', '--name', state['name'],
                       '--stateful', '--labels', 'purpose=nested-virt-probe')
        rules = cloud('security-group', 'rule', 'list', '--security-group-id', group)
        save(local / 'security-group-rules.json', rules)
        rule_items = rules if isinstance(rules, list) else rules.get('items', [])
        if any(rule.get('direction') == 'ingress' for rule in rule_items):
            raise RuntimeError('Unexpected ingress rules; refusing to create probe VM')
        if not any(rule.get('direction') == 'egress' for rule in rule_items):
            cloud('security-group', 'rule', 'create', '--security-group-id', group,
                  '--direction', 'egress', '--ether-type', 'IPv4', '--ip-range', '0.0.0.0/0')
        if args.ssh_source:
            cloud('security-group', 'rule', 'create', '--security-group-id', group,
                  '--direction', 'ingress', '--ether-type', 'IPv4', '--ip-range', args.ssh_source,
                  '--protocol-name', 'tcp', '--port-range-min', '22', '--port-range-max', '22')
        save(local / 'security-group-rules-active.json',
             cloud('security-group', 'rule', 'list', '--security-group-id', group))
        userdata = {
            'ssh_pwauth': False,
            'write_files': [{'path': '/root/nested-probe.sh', 'permissions': '0700',
                             'content': (ROOT / 'scripts/nested-probe.sh').read_text()}],
            'runcmd': [['bash', '-c',
                        'timeout 600 bash /root/nested-probe.sh > /root/nested-probe.log 2>&1; '
                        'status=$?; cat /root/nested-probe.log > /dev/ttyS0; '
                        'printf "SCF_WRAPPER_EXIT=%s\\n" "$status" > /dev/ttyS0; exit "$status"']],
        }
        cloudinit = local / 'probe-cloud-init.yaml'
        cloudinit.write_text(args.user_data_file.read_text() if args.user_data_file
                    else '#cloud-config\n' + json.dumps(userdata))
        public_ip = create('public-ip', 'public-ip', 'create', '--labels', 'purpose=nested-virt-probe')
        server = create('server', 'server', 'create', '--name', state['name'],
                        '--machine-type', args.machine_type, '--availability-zone', args.zone,
                        '--network-id', network, '--security-groups', group,
                        '--boot-volume-source-id', args.image_id, '--boot-volume-source-type', 'image',
                        '--boot-volume-size', str(args.boot_volume_size), '--boot-volume-performance-class', 'storage_premium_perf1',
                        '--boot-volume-delete-on-termination', '--agent-provisioning-policy', 'NEVER',
                        '--labels', 'purpose=nested-virt-probe', '--user-data', '@' + str(cloudinit))
        cloud('server', 'public-ip', 'attach', public_ip, '--server-id', server)
        print('Probe provisioned. Use status to read the console; do not recreate.', flush=True)
    elif args.action == 'status':
        print(json.dumps(state, indent=2))
        if 'server' in resources:
            details = cloud('server', 'describe', resources['server'])
            save(local / 'server-details.json', details)
            console = cloud('server', 'log', resources['server'], '--length', '2000')
            save(local / 'console.json', console)
            text = console if isinstance(console, str) else json.dumps(console)
            text = text.replace('\\n', '\n').replace('\\r', '')
            lines = text.splitlines()
            selected = [line for line in lines if any(marker in line for marker in
                        ['SCF_', 'Virtualization', 'Hypervisor', 'Model name', '/dev/kvm', 'failed to initialize kvm'])]
            print('\n'.join(selected) if selected else '\n'.join(lines[-12:]))
            print('Probe finished: ' + str('SCF_NESTED_END' in text))
    else:
        if state.get('pending_creation'):
            raise RuntimeError('Uncertain creation: reconcile pending_creation with inventory before cleanup')
        for kind in ['server', 'public-ip', 'security-group', 'network']:
            if kind in resources:
                cloud(kind, 'delete', resources[kind])
                state.setdefault('deleted_resources', {})[kind] = resources.pop(kind)
                save(manifest, state)
        for kind in ['server', 'volume', 'network', 'network-interface', 'public-ip', 'security-group']:
            save(local / (kind + '-after-cleanup.json'), cloud(kind, 'list'))
        state['cleanup_commands_completed'] = True
        save(manifest, state)
        print('Cleanup commands completed; verify saved inventories for leftover probe resources.')


if __name__ == '__main__':
    main()