#!/usr/bin/env python3
"""Gate one normal VMware guest shutdown on accepted delta and frozen source evidence."""
import argparse
import importlib.util
import json
from pathlib import Path
import ssl
import uuid

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim


spec = importlib.util.spec_from_file_location('pilot', Path(__file__).with_name('coriolis-pilot.py'))
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def validate_evidence(owner, acceptance, frozen, source_uuid):
    source_uuid = str(uuid.UUID(source_uuid))
    if (owner.get('pending_operation') or acceptance.get('result') != 'PASS'
            or acceptance.get('project_id') != owner.get('target_project_id')
            or acceptance.get('deployment_id') not in owner['deployments']):
        raise ValueError('Completed owned delta acceptance is required before source shutdown')
    latest = owner['executions'][-1]
    if (latest not in owner.get('delta_executions', [])
            or owner.get('rehearsal_executions', {}).get(latest) != acceptance['deployment_id']):
        raise ValueError('Acceptance must belong to the latest owned delta execution')
    if (frozen.get('phase') != 'FROZEN' or frozen.get('result') != 'PASS'
            or frozen.get('confirmed_source_uuid') != source_uuid):
        raise ValueError('Explicit matching frozen-source evidence is required')
    actual = uuid.UUID(frozen['source_dmi_uuid'])
    expected = uuid.UUID(source_uuid)
    if actual not in (expected, uuid.UUID(bytes_le=expected.bytes)):
        raise ValueError('Frozen guest DMI identity does not match the approved VMware source')
    for key in ('records', 'seed_records', 'writes', 'digest', 'data_uuid'):
        if frozen.get(key) != acceptance['boundary'][key]:
            raise ValueError('Frozen source differs from accepted delta boundary: ' + key)


def source_identity(vm, owner, source_uuid):
    if (vm.name != 'scf-relocate-app' or str(uuid.UUID(vm.config.uuid)) != str(uuid.UUID(source_uuid))
            or vm.config.instanceUuid != owner['instance_id'] or vm.snapshot is not None):
        raise ValueError('Exact snapshot-free VMware source identity is required')
    if (str(vm.runtime.powerState) != 'poweredOn'
            or str(vm.guest.toolsRunningStatus) != 'guestToolsRunning'):
        raise ValueError('Normal shutdown requires the source already running with VMware Tools')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--expected-source-uuid', required=True)
    parser.add_argument('--state', default='.local/pilot/coriolis.json')
    parser.add_argument('--acceptance', default='.local/pilot/delta-acceptance-agent.json')
    parser.add_argument('--freeze-evidence', default='.local/pilot/final-source-freeze.json')
    parser.add_argument('--record', default='.local/pilot/source-shutdown.json')
    parser.add_argument('--username', default='root')
    parser.add_argument('--password-file', default='.local/esxi/root-password')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    for value in (args.state, args.acceptance, args.freeze_evidence, args.record):
        if not Path(value).resolve().is_relative_to(Path('.local').resolve()):
            parser.error('Evidence and shutdown journal must remain under ignored .local')
    record_path = Path(args.record)
    if args.apply and record_path.exists():
        parser.error('Shutdown journal exists; inspect actual power state, never resend blindly')
    owner = json.loads(Path(args.state).read_text())
    acceptance = json.loads(Path(args.acceptance).read_text())
    frozen = json.loads(Path(args.freeze_evidence).read_text())
    validate_evidence(owner, acceptance, frozen, args.expected_source_uuid)
    connection = SmartConnect(host=args.host, user=args.username,
                              pwd=Path(args.password_file).read_text(), sslContext=ssl.create_default_context())
    try:
        content = connection.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            matches = [vm for vm in view.view if vm.name == 'scf-relocate-app'
                       and vm.config.instanceUuid == owner['instance_id']]
        finally:
            view.Destroy()
        if len(matches) != 1:
            raise ValueError('Approved VMware source must resolve uniquely')
        vm = matches[0]
        source_identity(vm, owner, args.expected_source_uuid)
        record = {'host': args.host, 'bios_uuid': str(uuid.UUID(args.expected_source_uuid)),
                  'instance_uuid': owner['instance_id'], 'vm_id': vm._moId,
                  'accepted_deployment': acceptance['deployment_id'], 'frozen_boundary': frozen,
                  'before_power_state': str(vm.runtime.powerState), 'hard_power_off': False}
        if args.apply:
            record['pending_operation'] = 'normal_guest_shutdown'
            pilot.save(record_path, record)
            vm.ShutdownGuest()
            record.pop('pending_operation')
            record['result'] = 'NORMAL_SHUTDOWN_REQUESTED'
            pilot.save(record_path, record)
        else:
            record['result'] = 'VALIDATED_PLAN'
        print(json.dumps(record, indent=2))
    finally:
        Disconnect(connection)


if __name__ == '__main__':
    main()