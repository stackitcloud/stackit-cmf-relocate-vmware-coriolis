#!/usr/bin/env python3
"""Scoped Coriolis pilot orchestration; preserve all pre-existing jobs."""
import argparse
import base64
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml
from jsonschema import Draft7Validator
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim


class CoriolisClient:
    def __init__(self, url, credentials_file, project='admin'):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Coriolis URL must be an HTTPS origin without credentials')
        self.base = url.rstrip('/')
        self.token = None
        config, _ = self.request('/api/config')
        config = config['config']
        paths = config['servicesUrls']
        identity = paths['keystone']
        api = paths['coriolis']
        for path in [identity, api]:
            parsed_path = urllib.parse.urlsplit(path)
            if not path.startswith('/') or parsed_path.netloc or parsed_path.query or parsed_path.fragment:
                raise ValueError('API service paths must remain on the trusted origin')
        credentials = yaml.safe_load(Path(credentials_file).read_text())
        _, headers = self.request(identity + '/auth/tokens', {
            'auth': {'identity': {'methods': ['password'], 'password': {'user': {
                'name': credentials['user'], 'password': credentials['password'],
                'domain': {'name': config['defaultUserDomain']}}}}, 'scope': 'unscoped'}})
        self.token = headers['X-Subject-Token']
        projects, _ = self.request(identity + '/auth/projects')
        matches = [item for item in projects['projects'] if item['name'] == project]
        if len(matches) != 1:
            raise ValueError('Requested Coriolis project must resolve uniquely')
        self.project_id = matches[0]['id']
        _, headers = self.request(identity + '/auth/tokens', {'auth': {
            'identity': {'methods': ['token'], 'token': {'id': self.token}},
            'scope': {'project': {'id': self.project_id}}}})
        self.token = headers['X-Subject-Token']
        self.prefix = api.rstrip('/') + '/' + self.project_id

    def request(self, path, body=None, method=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Requests must remain on the trusted API origin')
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['X-Auth-Token'] = self.token
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode() if body is not None else None,
            headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
            return (json.loads(content) if content else None), response.headers

    def api(self, path, body=None, method=None):
        result, _ = self.request(self.prefix + path, body, method)
        return result


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    temporary.replace(path)


def inventory(client, source_endpoint):
    result = {'coriolis_project_id': client.project_id}
    for resource in ['endpoints', 'transfers', 'deployments']:
        fields = {'id', 'name', 'type', 'last_execution_status', 'status'}
        result[resource] = [{key: value for key, value in item.items() if key in fields}
                            for item in client.api('/' + resource)[resource]]
    endpoints = [endpoint for endpoint in result['endpoints'] if endpoint['id'] == source_endpoint]
    if len(endpoints) != 1 or endpoints[0]['type'] != 'vmware_vsphere':
        raise ValueError('Expected VMware source endpoint was not found uniquely')
    result['source_connection'] = client.api(
        '/endpoints/' + source_endpoint + '/actions', {'validate-connection': None})['validate-connection']['valid']
    instances = client.api('/endpoints/' + source_endpoint + '/instances?refresh=true&limit=100')['instances']
    fields = {'id', 'name', 'os_type', 'power_state', 'status'}
    result['source_instances'] = [{key: value for key, value in item.items() if key in fields}
                                  for item in instances]
    return result


def prepare_transfer(client, args):
    network = json.loads(Path(args.network_state).read_text())
    if network['project_id'] != args.confirm_project_id or not network.get('carrier_nic_attached'):
        raise ValueError('Target project confirmation or carrier attachment is missing')
    if network.get('pending_operation'):
        raise ValueError('Resolve pending network operations before migration')
    source = inventory(client, args.source_endpoint)
    matches = [instance for instance in source['source_instances']
               if instance.get('name') == 'scf-relocate-app' and instance['id'] == args.instance_id]
    if len(matches) != 1 or not source['source_connection']:
        raise ValueError('Licensed application source must resolve uniquely and validate')
    source_environment = {
        'export_transfer_mechanism': 'openvixdisklib', 'automatically_enable_cbt': False,
        'verify_disk_integrity': True, 'skip_nfc_validation': False}
    target_environment = {
        'project': network['project_id'], 'network_map': {'VM Network': network['resources']['network']},
        'migr_network': network['resources']['network'], 'migr_machine_type': 'c3i.2',
        'machine_type': 'c3i.2', 'availability_zone': 'eu01-1', 'set_dhcp': True,
        'migr_image_map': {'linux': args.worker_image}, 'migr_worker_use_public_ip': False,
        'use_public_ip': False, 'preserve_fixed_ips': False, 'retain_user_credentials': True,
        'security_groups': [network['resources']['security_group']], 'data_transfer_mechanism': 'HTTPS',
        'volumes_are_zeroed': False, 'delete_disks_on_server_termination': False}
    schemas = [('/providers/vmware_vsphere/schemas/8', 'source_environment_schema', source_environment),
               ('/providers/stackit/schemas/4', 'destination_environment_schema', target_environment)]
    for route, name, value in schemas:
        schema = client.api(route)['schemas'][name]
        Draft7Validator(schema).validate(value)
    definition = {
        'scenario': 'live_migration', 'origin_endpoint_id': args.source_endpoint,
        'instances': [args.instance_id], 'source_environment': source_environment,
        'destination_environment': target_environment,
        'network_map': target_environment['network_map'], 'clone_disks': True,
        'skip_os_morphing': False, 'notes': 'SCF Relocate Pilot: owned isolated VMware application test'}
    return network, source, definition


def create_transfer(client, args):
    network, baseline, definition = prepare_transfer(client, args)
    if args.dry_run:
        return {'result': 'VALIDATED_PLAN', 'definition': definition,
                'target_project': network['project_id'], 'public_workers': False}
    if Path(args.state).exists():
        raise ValueError('Pilot journal already exists; inspect status rather than creating again')
    endpoint_name = 'scf-relocate-pilot-stackit'
    if any(endpoint.get('name') == endpoint_name for endpoint in baseline['endpoints']):
        raise ValueError('Unowned pilot endpoint already exists; refusing implicit adoption')
    if not args.organization_id:
        raise ValueError('Explicit STACKIT organization ID is required')
    connection = {'organization_id': args.organization_id, 'project_id': network['project_id'],
                  'region_name': network['region'],
                  'service_account_key': base64.b64encode(Path(args.stackit_key).expanduser().read_bytes()).decode()}
    schema = client.api('/providers/stackit/schemas/16')['schemas']['connection_info_schema']
    Draft7Validator(schema).validate(connection)
    state = {'coriolis_project_id': client.project_id, 'target_project_id': network['project_id'],
             'source_endpoint': args.source_endpoint, 'instance_id': args.instance_id,
             'baseline_job_ids': {kind: sorted(item['id'] for item in baseline[kind])
                                  for kind in ['transfers', 'deployments']},
             'resources': {}, 'executions': [], 'deployments': [], 'pending_operation': 'create_endpoint'}
    save(args.state, state)
    regions = client.api('/regions')['regions']
    regions = [region for region in regions if region.get('name') == 'Public' and region.get('enabled', True)]
    if len(regions) != 1:
        raise ValueError('Enabled Public worker region must resolve uniquely')
    endpoint = client.api('/endpoints', {'endpoint': {
        'name': endpoint_name, 'type': 'stackit',
        'description': 'Owned SCF Relocate pilot; dedicated project and private workers',
        'connection_info': connection, 'mapped_regions': [regions[0]['id']]}})['endpoint']
    state['resources']['target_endpoint'] = endpoint['id']
    state['pending_operation'] = 'validate_target_endpoint'
    save(args.state, state)
    validation = client.api('/endpoints/' + endpoint['id'] + '/actions', {'validate-connection': None})
    if not validation['validate-connection']['valid']:
        raise ValueError('New target endpoint failed validation; inspect before retrying')
    definition['destination_endpoint_id'] = endpoint['id']
    state['definition'] = definition
    state['pending_operation'] = 'create_transfer'
    save(args.state, state)
    transfer = client.api('/transfers', {'transfer': definition})['transfer']
    state['resources']['transfer'] = transfer['id']
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'TRANSFER_CREATED', 'target_endpoint': endpoint['id'], 'transfer': transfer['id']}


def own_state(client, args):
    state = json.loads(Path(args.state).read_text())
    if (state['coriolis_project_id'] != client.project_id
            or state['source_endpoint'] != args.source_endpoint):
        raise ValueError('Pilot ownership journal does not match API scope')
    return state


def deployment_evidence(client, args):
    state = own_state(client, args)
    if args.deployment_id not in state['deployments']:
        raise ValueError('Evidence requires an explicitly owned deployment ID')
    deployment = client.api('/deployments/' + args.deployment_id
                            + '?include_info=true&include_task_info=true')['deployment']
    if (deployment.get('id') != args.deployment_id
            or deployment.get('transfer_id') != state['resources']['transfer']
            or deployment.get('last_execution_status') != 'COMPLETED'
            or state['instance_id'] not in deployment.get('info', {})):
        raise ValueError('Completed matching deployment with source instance info is required')
    save(args.output, deployment)
    return {'result': 'DEPLOYMENT_EVIDENCE_SAVED', 'deployment': args.deployment_id,
            'output': args.output}


def status(client, args):
    state = own_state(client, args)
    result = {'pending_operation': state.get('pending_operation'), 'resources': state['resources']}
    if 'transfer' in state['resources']:
        transfer = client.api('/transfers/' + state['resources']['transfer'] + '?include_task_info=true')['transfer']
        result['transfer_status'] = transfer.get('last_execution_status')
        result['executions'] = []
        for execution_id in state['executions']:
            execution = client.api('/transfers/' + state['resources']['transfer']
                                   + '/executions/' + execution_id + '?include_task_info=true')['execution']
            result['executions'].append({
                'id': execution['id'], 'status': execution['status'],
                'tasks': [{key: task.get(key) for key in ['id', 'task_type', 'status']}
                          for task in execution.get('tasks', [])]})
    for deployment_id in state['deployments']:
        deployment = client.api('/deployments/' + deployment_id + '?include_task_info=true')['deployment']
        summary = {key: deployment.get(key) for key in ['id', 'status', 'last_execution_status']}
        summary['tasks'] = [{key: task.get(key) for key in ['id', 'task_type', 'status']}
                            for task in deployment.get('tasks', [])]
        result.setdefault('deployments', []).append(summary)
    return result


def execute_transfer(client, args):
    state = own_state(client, args)
    if state.get('pending_operation'):
        raise ValueError('Resolve pending pilot operations before creating an execution')
    if state['executions']:
        raise ValueError('This initial-copy action has already run; inspect before scheduling a delta')
    state['pending_operation'] = 'execute_initial_copy'
    save(args.state, state)
    execution = client.api('/transfers/' + state['resources']['transfer'] + '/executions', {
        'execution': {'shutdown_instances': False, 'auto_deploy': False}})['execution']
    state['executions'].append(execution['id'])
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'INITIAL_COPY_STARTED', 'execution': execution['id'], 'source_shutdown': False}


def deploy_rehearsal(client, args, after_delta=False):
    state = own_state(client, args)
    if state.get('pending_operation') or not state['executions']:
        raise ValueError('Existing or pending deployment requires inspection; do not create another')
    execution_id = state['executions'][-1]
    if after_delta:
        if (not state['deployments'] or execution_id not in state.get('delta_executions', [])
                or execution_id in state.get('rehearsal_executions', {})):
            raise ValueError('Delta rehearsal requires an undeployed journaled delta execution')
    elif state['deployments']:
        raise ValueError('Initial rehearsal has already been created')
    transfer_id = checked_transfer(client, state, 'COMPLETED')
    if after_delta:
        execution = client.api('/transfers/' + transfer_id + '/executions/' + execution_id
                               + '?include_task_info=true')['execution']
        cleanup = [task for task in execution.get('tasks', [])
                   if task.get('task_type', '').startswith('DELETE_TRANSFER_')]
        if (execution.get('id') != execution_id or execution.get('status') != 'COMPLETED'
                or {task.get('task_type') for task in cleanup} != {
                    'DELETE_TRANSFER_SOURCE_RESOURCES', 'DELETE_TRANSFER_TARGET_RESOURCES'}
                or any(task.get('status') != 'COMPLETED' for task in cleanup)):
            raise ValueError('Delta execution and both resource-cleanup tasks must be completed')
        for deployment_id in state['deployments']:
            previous = client.api('/deployments/' + deployment_id)['deployment']
            if (previous.get('id') != deployment_id or previous.get('transfer_id') != transfer_id
                    or previous.get('last_execution_status') != 'COMPLETED'):
                raise ValueError('Previous rehearsal must belong to this transfer and be completed')
    state['pending_operation'] = 'deploy_delta_rehearsal' if after_delta else 'deploy_rehearsal'
    save(args.state, state)
    deployment = client.api('/deployments', {'deployment': {
        'transfer_id': transfer_id, 'clone_disks': True,
        'force': False, 'skip_os_morphing': False}})['deployment']
    state['deployments'].append(deployment['id'])
    state.setdefault('rehearsal_executions', {})[execution_id] = deployment['id']
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'REHEARSAL_STARTED', 'deployment': deployment['id'], 'clone_disks': True}


def deploy_delta_rehearsal(client, args):
    return deploy_rehearsal(client, args, after_delta=True)


def checked_transfer(client, state, expected_status):
    transfer_id = state['resources']['transfer']
    transfer = client.api('/transfers/' + transfer_id)['transfer']
    if (transfer.get('origin_endpoint_id') != state['source_endpoint']
            or transfer.get('destination_endpoint_id') != state['resources']['target_endpoint']
            or transfer.get('instances') != [state['instance_id']]
            or transfer.get('last_execution_status') != expected_status):
        raise ValueError('Live transfer identity or terminal state does not match the owned pilot')
    return transfer_id


def retry_transfer(client, args):
    state = own_state(client, args)
    if state.get('pending_operation') or not state['executions'] or state['deployments']:
        raise ValueError('Retry requires a known failed execution and no pending operation or deployment')
    transfer_id = checked_transfer(client, state, 'ERROR')
    execution_id = state['executions'][-1]
    execution = client.api('/transfers/' + transfer_id + '/executions/' + execution_id
                           + '?include_task_info=true')['execution']
    cleanup = [task for task in execution.get('tasks', [])
               if task.get('task_type', '').startswith('DELETE_TRANSFER_')]
    if (execution.get('id') != execution_id or execution.get('status') != 'ERROR'
            or {task.get('task_type') for task in cleanup} != {
                'DELETE_TRANSFER_SOURCE_RESOURCES', 'DELETE_TRANSFER_TARGET_RESOURCES'}
            or any(task.get('status') != 'COMPLETED' for task in cleanup)):
        raise ValueError('Previous failed execution must have both completed resource-cleanup tasks')
    state['pending_operation'] = 'retry_initial_copy'
    save(args.state, state)
    retry = client.api('/transfers/' + transfer_id + '/executions', {
        'execution': {'shutdown_instances': False, 'auto_deploy': False}})['execution']
    state['executions'].append(retry['id'])
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'INITIAL_COPY_RETRY_STARTED', 'execution': retry['id'], 'source_shutdown': False}


def sync_transfer(client, args):
    state = own_state(client, args)
    if state.get('pending_operation') or not state['executions']:
        raise ValueError('Delta sync requires a completed owned copy and no pending operation')
    transfer_id = checked_transfer(client, state, 'COMPLETED')
    execution_id = state['executions'][-1]
    execution = client.api('/transfers/' + transfer_id + '/executions/' + execution_id)['execution']
    if execution.get('id') != execution_id or execution.get('status') != 'COMPLETED':
        raise ValueError('Most recent journaled execution must be completed before delta sync')
    for deployment_id in state['deployments']:
        deployment = client.api('/deployments/' + deployment_id)['deployment']
        if deployment.get('last_execution_status') != 'COMPLETED':
            raise ValueError('Rehearsal deployment must finish successfully before delta sync')
    state['pending_operation'] = 'execute_delta_sync'
    save(args.state, state)
    delta = client.api('/transfers/' + transfer_id + '/executions', {
        'execution': {'shutdown_instances': False, 'auto_deploy': False}})['execution']
    state['executions'].append(delta['id'])
    state.setdefault('delta_executions', []).append(delta['id'])
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'DELTA_SYNC_STARTED', 'execution': delta['id'], 'source_shutdown': False}


def frozen_cutover_gate(state, args):
    if not args.confirm_source_uuid or not args.esxi_host:
        raise ValueError('Final cutover requires explicit source BIOS UUID and verified ESXi host')
    acceptance = json.loads(Path(args.acceptance_evidence).read_text())
    frozen = json.loads(Path(args.freeze_evidence).read_text())
    expected = uuid.UUID(args.confirm_source_uuid)
    if (acceptance.get('result') != 'PASS'
            or acceptance.get('project_id') != state['target_project_id']
            or acceptance.get('deployment_id') not in state['deployments']
            or frozen.get('phase') != 'FROZEN' or frozen.get('result') != 'PASS'
            or frozen.get('confirmed_source_uuid') != str(expected)
            or uuid.UUID(frozen['source_dmi_uuid']) not in (expected, uuid.UUID(bytes_le=expected.bytes))):
        raise ValueError('Accepted own rehearsal and matching frozen-source identity are required')
    for key in ('records', 'seed_records', 'writes', 'digest', 'data_uuid'):
        if frozen.get(key) != acceptance['boundary'][key]:
            raise ValueError('Frozen source differs from accepted delta: ' + key)
    return frozen


def verified_source_power_state(state, args):
    connection = SmartConnect(host=args.esxi_host, user='root',
                              pwd=Path(args.source_password_file).read_text(),
                              sslContext=ssl.create_default_context())
    try:
        content = connection.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            matches = [vm for vm in view.view if vm.name == 'scf-relocate-app'
                       and vm.config.instanceUuid == state['instance_id']
                       and str(uuid.UUID(vm.config.uuid)) == str(uuid.UUID(args.confirm_source_uuid))]
        finally:
            view.Destroy()
        if len(matches) != 1 or matches[0].snapshot is not None:
            raise ValueError('Exact snapshot-free VMware source must resolve uniquely')
        return str(matches[0].runtime.powerState)
    finally:
        Disconnect(connection)


def final_transfer(client, args):
    state = own_state(client, args)
    if state.get('pending_operation') or state.get('final_execution') or not state['executions']:
        raise ValueError('Final copy must be explicitly fresh, owned and unambiguous')
    latest = state['executions'][-1]
    acceptance = json.loads(Path(args.acceptance_evidence).read_text())
    if (latest not in state.get('delta_executions', [])
            or state.get('rehearsal_executions', {}).get(latest) != acceptance.get('deployment_id')):
        raise ValueError('Accepted rehearsal must match the latest owned delta')
    frozen = frozen_cutover_gate(state, args)
    if verified_source_power_state(state, args) != 'poweredOff':
        raise ValueError('Source must already be normally shut down before the final copy')
    transfer_id = checked_transfer(client, state, 'COMPLETED')
    execution = client.api('/transfers/' + transfer_id + '/executions/' + latest)['execution']
    if execution.get('id') != latest or execution.get('status') != 'COMPLETED':
        raise ValueError('Latest journaled delta must be completed before final copy')
    state['pending_operation'] = 'execute_final_copy'
    state['final_frozen_boundary'] = frozen
    save(args.state, state)
    execution = client.api('/transfers/' + transfer_id + '/executions', {
        'execution': {'shutdown_instances': False, 'auto_deploy': False}})['execution']
    state['executions'].append(execution['id'])
    state['final_execution'] = execution['id']
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'FINAL_COPY_STARTED', 'execution': execution['id'],
            'source_power_state': 'poweredOff', 'auto_deploy': False}


def deploy_final(client, args):
    state = own_state(client, args)
    if (state.get('pending_operation') or state.get('final_deployment')
            or not state.get('final_execution') or state['executions'][-1] != state['final_execution']):
        raise ValueError('Final deployment requires one unambiguous completed final execution')
    frozen = frozen_cutover_gate(state, args)
    if frozen != state.get('final_frozen_boundary'):
        raise ValueError('Final frozen boundary changed after final-copy submission')
    if verified_source_power_state(state, args) != 'poweredOff':
        raise ValueError('Retained source must remain powered off before final deployment')
    transfer_id = checked_transfer(client, state, 'COMPLETED')
    execution = client.api('/transfers/' + transfer_id + '/executions/' + state['final_execution']
                           + '?include_task_info=true')['execution']
    cleanup = [task for task in execution.get('tasks', [])
               if task.get('task_type', '').startswith('DELETE_TRANSFER_')]
    if (execution.get('id') != state['final_execution'] or execution.get('status') != 'COMPLETED'
            or {task.get('task_type') for task in cleanup} != {
                'DELETE_TRANSFER_SOURCE_RESOURCES', 'DELETE_TRANSFER_TARGET_RESOURCES'}
            or any(task.get('status') != 'COMPLETED' for task in cleanup)):
        raise ValueError('Final copy and both cleanup tasks must be completed')
    state['pending_operation'] = 'deploy_final'
    save(args.state, state)
    deployment = client.api('/deployments', {'deployment': {
        'transfer_id': transfer_id, 'clone_disks': True, 'force': False,
        'skip_os_morphing': False}})['deployment']
    state['deployments'].append(deployment['id'])
    state['final_deployment'] = deployment['id']
    state.setdefault('rehearsal_executions', {})[state['final_execution']] = deployment['id']
    state.pop('pending_operation')
    save(args.state, state)
    return {'result': 'FINAL_DEPLOYMENT_STARTED', 'deployment': deployment['id'],
            'source_power_state': 'poweredOff', 'traffic_switch': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inventory', 'create-transfer', 'execute-transfer', 'retry-transfer', 'sync-transfer', 'deploy-rehearsal', 'deploy-delta-rehearsal', 'final-transfer', 'deploy-final', 'deployment-evidence', 'status'])
    parser.add_argument('--url', required=True)
    parser.add_argument('--credentials-file', required=True)
    parser.add_argument('--coriolis-project', default='admin')
    parser.add_argument('--source-endpoint', required=True)
    parser.add_argument('--output', default='.local/coriolis/pilot-inventory.json')
    parser.add_argument('--state', default='.local/pilot/coriolis.json')
    parser.add_argument('--network-state', default='.local/pilot/network.json')
    parser.add_argument('--confirm-project-id')
    parser.add_argument('--instance-id')
    parser.add_argument('--organization-id')
    parser.add_argument('--stackit-key', default='~/.ssh/cf-migration-sa.json')
    parser.add_argument('--worker-image', default='cc5ec888-ee66-4319-8b69-1afccceadeb9')
    parser.add_argument('--confirm-source-uuid')
    parser.add_argument('--esxi-host')
    parser.add_argument('--source-password-file', default='.local/esxi/root-password')
    parser.add_argument('--deployment-id')
    parser.add_argument('--acceptance-evidence', default='.local/pilot/delta-acceptance-agent-retry.json')
    parser.add_argument('--freeze-evidence', default='.local/pilot/final-source-freeze.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    for path in [args.state, args.output, args.network_state, args.acceptance_evidence, args.freeze_evidence]:
        if not Path(path).resolve().is_relative_to(Path('.local').resolve()):
            parser.error('Pilot state and evidence must remain inside the ignored .local directory')
    if args.dry_run and args.action != 'create-transfer':
        parser.error('--dry-run applies only to create-transfer')
    try:
        client = CoriolisClient(args.url, args.credentials_file, args.coriolis_project)
        if args.action == 'inventory':
            result = inventory(client, args.source_endpoint)
        else:
            actions = {'create-transfer': create_transfer, 'execute-transfer': execute_transfer,
                       'retry-transfer': retry_transfer, 'sync-transfer': sync_transfer,
                       'deploy-rehearsal': deploy_rehearsal,
                       'deploy-delta-rehearsal': deploy_delta_rehearsal,
                       'final-transfer': final_transfer, 'deploy-final': deploy_final,
                       'deployment-evidence': deployment_evidence, 'status': status}
            result = actions[args.action](client, args)
        if args.action != 'deployment-evidence':
            save(args.output, result)
        print(json.dumps(result, indent=2))
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Coriolis HTTP {error.code}; response body and credentials not logged') from None


if __name__ == '__main__':
    main()