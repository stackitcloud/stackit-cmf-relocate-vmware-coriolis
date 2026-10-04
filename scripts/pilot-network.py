#!/usr/bin/env python3
"""Create the private pilot network with a journal before every cloud mutation."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess


def public_target_plan(network, pilot, deployment, server, source):
    source = ipaddress.ip_network(source, strict=True)
    if source.version != 4 or source.prefixlen != 32 or not source.network_address.is_global:
        raise ValueError('Public SSH requires one explicitly approved global IPv4 /32')
    if (network.get('pending_operation') or pilot.get('pending_operation')
            or pilot['target_project_id'] != network['project_id']
            or deployment['id'] not in pilot['deployments']
            or deployment.get('last_execution_status') != 'COMPLETED'
            or deployment.get('transfer_id') != pilot['resources']['transfer']):
        raise ValueError('Completed owned deployment and matching project are required')
    info = deployment['info'][pilot['instance_id']]['instance_deployment_info']
    if (server.get('status') != 'ACTIVE' or server.get('name') != info['instance_name']
            or len(server['nics']) != 1
            or {nic['nicId'] for nic in server['nics']} != set(info['nic_ids'])
            or set(server['volumes']) != {volume['volume_id'] for volume in info['volumes_info']}):
        raise ValueError('Live server NICs/volumes do not match the owned deployment')
    nic = server['nics'][0]
    if (nic['networkId'] != network['resources']['network'] or not nic.get('nicSecurity')
            or network['resources']['security_group'] not in nic['securityGroups']):
        raise ValueError('Expected private network and enforced owned security group are required')
    return {'server_id': server['id'], 'deployment_id': deployment['id'],
            'nic_id': nic['nicId'], 'ssh_source': str(source), 'resources': {}}


def check_public_ingress(rules, approved_sources, group_id):
    for rule in rules:
        if rule.get('direction') != 'ingress':
            continue
        if rule.get('remoteSecurityGroupId') == group_id and not rule.get('ipRange'):
            continue
        if (rule.get('ipRange') in approved_sources
                and rule.get('protocol', {}).get('name') == 'tcp'
                and rule.get('portRange') == {'min': 22, 'max': 22}
                and rule.get('ethertype') == 'IPv4'):
            continue
        raise ValueError('Unexpected ingress would be exposed; refusing public IP association')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'resume', 'status', 'publish-target'])
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--carrier-server', required=True)
    parser.add_argument('--region', default='eu01')
    parser.add_argument('--credentials', default='~/.ssh/cf-migration-sa.json')
    parser.add_argument('--cli', default='.local/tools/stackit')
    parser.add_argument('--state', default='.local/pilot/network.json')
    parser.add_argument('--target-server')
    parser.add_argument('--ssh-source')
    parser.add_argument('--pilot-state', default='.local/pilot/coriolis.json')
    parser.add_argument('--deployment-evidence', default='.local/pilot/rehearsal-deployment.json')
    args = parser.parse_args()
    os.umask(0o077)
    path = Path(args.state)
    if any(not Path(value).resolve().is_relative_to(Path('.local').resolve())
            for value in [args.state, args.pilot_state, args.deployment_evidence]):
        parser.error('State must remain inside the ignored .local directory')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = json.loads(path.read_text()) if path.exists() else {
        'project_id': args.project_id, 'region': args.region, 'carrier_server': args.carrier_server,
        'name': 'scf-relocate-pilot', 'prefix': '10.77.242.0/24',
        'carrier_address': '10.77.242.50', 'resources': {}}
    if (state['project_id'], state['region'], state['carrier_server']) != (
            args.project_id, args.region, args.carrier_server):
        parser.error('Existing resource journal does not match the requested scope')

    def save():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(path)

    auth = subprocess.run([args.cli, 'auth', 'activate-service-account',
                           '--service-account-key-path', str(Path(args.credentials).expanduser()),
                           '--only-print-access-token'], capture_output=True, text=True, timeout=90)
    if auth.returncode:
        raise SystemExit('Lab service-account authentication failed; credentials not logged')
    environment = dict(os.environ, STACKIT_ACCESS_TOKEN=auth.stdout.strip())

    def cloud(*command):
        result = subprocess.run([args.cli, *command, '--project-id', args.project_id,
                                 '--region', args.region, '--output-format', 'json', '--assume-yes'],
                                env=environment, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError('STACKIT operation failed: ' + ' '.join(command[:2]))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def create(key, *command):
        state['pending_operation'] = key
        save()
        result = cloud(*command)
        state['resources'][key] = result['id']
        state.pop('pending_operation')
        save()
        return result

    if args.action == 'publish-target':
        if not args.target_server or not args.ssh_source:
            parser.error('Public target access requires --target-server and --ssh-source')
        pilot = json.loads(Path(args.pilot_state).read_text())
        deployment = json.loads(Path(args.deployment_evidence).read_text())
        server = cloud('server', 'describe', args.target_server)
        plan = public_target_plan(state, pilot, deployment, server, args.ssh_source)
        records = state.setdefault('public_targets', {})
        record = records.setdefault(server['id'], plan)
        if any(record.get(key) != plan[key] for key in ['server_id', 'deployment_id', 'nic_id', 'ssh_source']):
            raise RuntimeError('Existing public-access journal differs; reconcile before changing access')
        approved = {state['carrier_address'] + '/32', record['ssh_source']}
        approved.update(value['ssh_source'] for value in records.values())
        for group_id in server['nics'][0]['securityGroups']:
            rules = cloud('security-group', 'rule', 'list', '--security-group-id', group_id)
            rules = rules if isinstance(rules, list) else rules.get('items', [])
            check_public_ingress(rules, approved, group_id)
        group_id = state['resources']['security_group']
        if 'ssh_rule' not in record['resources']:
            rules = cloud('security-group', 'rule', 'list', '--security-group-id', group_id)
            rules = rules if isinstance(rules, list) else rules.get('items', [])
            matches = [rule for rule in rules if rule.get('direction') == 'ingress'
                       and rule.get('ipRange') == record['ssh_source']]
            owned_rules = {value['resources'].get('ssh_rule') for value in records.values()}
            if matches:
                if len(matches) != 1 or matches[0]['id'] not in owned_rules:
                    raise RuntimeError('Matching unowned SSH rule exists; refusing implicit adoption')
                record['resources']['ssh_rule'] = matches[0]['id']
                save()
            else:
                state['pending_operation'] = 'publish_target_ssh_rule'
                save()
                rule = cloud('security-group', 'rule', 'create', '--security-group-id', group_id,
                             '--direction', 'ingress', '--ether-type', 'IPv4',
                             '--ip-range', record['ssh_source'], '--protocol-name', 'tcp',
                             '--port-range-min', '22', '--port-range-max', '22',
                             '--description', 'SCF owned target SSH from approved endpoint')
                record['resources']['ssh_rule'] = rule['id']
                state.pop('pending_operation')
                save()
        if 'public_ip' not in record['resources']:
            state['pending_operation'] = 'publish_target_public_ip'
            save()
            address = cloud('public-ip', 'create', '--associated-resource-id', record['nic_id'],
                            '--labels', 'purpose=relocate-pilot,target-server=' + server['id'])
            record['resources']['public_ip'] = address['id']
            state.pop('pending_operation')
            save()
        address = cloud('public-ip', 'describe', record['resources']['public_ip'])
        record['public_address'] = address['ip']
        save()
        print(json.dumps(record, indent=2))
        return

    if args.action in ('create', 'resume'):
        if args.action == 'create' and (state['resources'] or state.get('pending_operation')):
            raise RuntimeError('Existing or uncertain resources: inspect status, do not recreate')
        if args.action == 'resume':
            if state.get('pending_operation') != 'carrier_nic':
                raise RuntimeError('Only the recorded reserved-address NIC failure can be resumed')
            interfaces = cloud('network-interface', 'list')
            interfaces = interfaces if isinstance(interfaces, list) else interfaces.get('items', [])
            matches = [interface for interface in interfaces
                       if interface.get('networkId') == state['resources']['network']]
            if any(interface.get('type') == 'server' for interface in matches):
                raise RuntimeError('Unexpected server NIC already exists; reconcile manually')
            if not any(interface.get('ipv4') == state['carrier_address']
                       and interface.get('type') == 'metadata' for interface in matches):
                raise RuntimeError('Failure is not explained by a reserved metadata address')
            if any(interface.get('ipv4') == '10.77.242.50' for interface in matches):
                raise RuntimeError('Replacement carrier address is already allocated')
            old_rule = state['resources']['ssh_rule']
            state['pending_operation'] = 'replace_reserved_address_rule'
            save()
            cloud('security-group', 'rule', 'delete', old_rule,
                  '--security-group-id', state['resources']['security_group'])
            state.setdefault('removed_rules', []).append(old_rule)
            state['resources'].pop('ssh_rule')
            state['carrier_address'] = '10.77.242.50'
            state.pop('pending_operation')
            save()
        network_list = cloud('network', 'list')
        networks = network_list if isinstance(network_list, list) else network_list.get('items', [])
        prefix = ipaddress.ip_network(state['prefix'])
        for network in networks:
            if network['id'] == state['resources'].get('network'):
                continue
            for existing in network.get('ipv4', {}).get('prefixes', []):
                if prefix.overlaps(ipaddress.ip_network(existing)):
                    raise RuntimeError('Pilot subnet overlaps an existing project network')
        carrier = cloud('server', 'describe', args.carrier_server)
        if carrier.get('status') != 'ACTIVE':
            raise RuntimeError('Expected existing carrier is not ACTIVE')
        network = {'id': state['resources']['network']} if 'network' in state['resources'] else create(
            'network', 'network', 'create', '--name', state['name'],
            '--ipv4-prefix', state['prefix'], '--labels', 'purpose=relocate-pilot')
        group = {'id': state['resources']['security_group']} if 'security_group' in state['resources'] else create(
            'security_group', 'security-group', 'create', '--name', state['name'],
            '--stateful', '--labels', 'purpose=relocate-pilot')
        rules = cloud('security-group', 'rule', 'list', '--security-group-id', group['id'])
        rules = rules if isinstance(rules, list) else rules.get('items', [])
        if any(rule.get('direction') == 'ingress' for rule in rules):
            raise RuntimeError('Unexpected ingress on the new target security group')
        state['pending_operation'] = 'ssh_rule'
        save()
        rule = cloud('security-group', 'rule', 'create', '--security-group-id', group['id'],
                     '--direction', 'ingress', '--ether-type', 'IPv4',
                     '--ip-range', state['carrier_address'] + '/32', '--protocol-name', 'tcp',
                     '--port-range-min', '22', '--port-range-max', '22')
        state['resources']['ssh_rule'] = rule['id']
        state.pop('pending_operation')
        save()
        interface = create('carrier_nic', 'network-interface', 'create',
                           '--network-id', network['id'], '--ipv4', state['carrier_address'],
                           '--name', 'scf-relocate-pilot-carrier', '--security-groups', group['id'],
                           '--labels', 'purpose=relocate-pilot')
        state['carrier_mac'] = interface['mac']
        state['pending_operation'] = 'attach_carrier_nic'
        save()
        cloud('server', 'network-interface', 'attach', '--network-interface-id', interface['id'],
              '--server-id', args.carrier_server)
        state.pop('pending_operation')
        state['carrier_nic_attached'] = True
        save()
    print(json.dumps(state, indent=2))


if __name__ == '__main__':
    main()