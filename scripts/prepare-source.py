#!/usr/bin/env python3
"""Inspect or prepare exactly one VMware pilot; never power off the source."""
import argparse
import json
import os
from pathlib import Path
import ssl
import uuid

from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim


def inspect_vm(vm):
    disks = [device for device in vm.config.hardware.device
             if isinstance(device, vim.vm.device.VirtualDisk)]
    return {
        'name': vm.name,
        'bios_uuid': vm.config.uuid,
        'instance_uuid': vm.config.instanceUuid,
        'power_state': str(vm.runtime.powerState),
        'tools_status': str(vm.guest.toolsRunningStatus),
        'cbt_enabled': vm.config.changeTrackingEnabled,
        'has_snapshots': vm.snapshot is not None,
        'disks': [{'key': disk.key, 'capacity_bytes': disk.capacityInKB * 1024,
                   'change_id': getattr(disk.backing, 'changeId', None)}
                  for disk in disks],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--vm', default='scf-relocate-app')
    parser.add_argument('--username', default='root')
    parser.add_argument('--password-file', default='.local/esxi/root-password')
    parser.add_argument('--output', default='.local/esxi/source-readiness.json')
    parser.add_argument('--assign-instance-uuid', action='store_true')
    parser.add_argument('--enable-cbt', action='store_true')
    parser.add_argument('--snapshot-test', action='store_true')
    parser.add_argument('--configure-export-role', action='store_true')
    args = parser.parse_args()
    if args.vm != 'scf-relocate-app':
        parser.error('This pilot helper is restricted to scf-relocate-app')
    if args.configure_export_role and args.username != 'root':
        parser.error('Role preparation requires the existing lab root account')
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists() and (args.assign_instance_uuid or args.enable_cbt or args.snapshot_test
                            or args.configure_export_role):
        parser.error('Use a fresh output path; inspect prior evidence before retrying')
    evidence = {'result': 'IN_PROGRESS', 'host': args.host, 'vm': args.vm}

    def save():
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(evidence, stream, indent=2)
            stream.write('\n')

    connection = SmartConnect(host=args.host, user=args.username,
                              pwd=Path(args.password_file).read_text(),
                              sslContext=ssl.create_default_context())
    try:
        content = connection.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            matches = [vm for vm in view.view if vm.name == args.vm]
        finally:
            view.Destroy()
        if len(matches) != 1:
            raise RuntimeError('Pilot VM must resolve uniquely')
        vm = matches[0]
        evidence['before'] = inspect_vm(vm)
        evidence['licenses'] = [{'name': license.name, 'edition': license.editionKey}
                                for license in content.licenseManager.licenses]
        save()
        if args.configure_export_role:
            privileges = ['System.Anonymous', 'System.View', 'System.Read',
                          'VirtualMachine.State.CreateSnapshot', 'VirtualMachine.State.RemoveSnapshot',
                          'VirtualMachine.Config.ChangeTracking', 'VirtualMachine.Config.DiskLease',
                          'VirtualMachine.Provisioning.DiskRandomRead', 'VirtualMachine.Provisioning.GetVmFiles',
                          'Datastore.Browse', 'Datastore.FileManagement']
            manager = content.authorizationManager
            available = {privilege.privId for privilege in manager.privilegeList}
            if not set(privileges).issubset(available):
                raise RuntimeError('Requested export privileges are not supported by this host')
            role_name = 'SCF Relocate Pilot Export'
            roles = [role for role in manager.roleList if role.name == role_name]
            if roles and set(roles[0].privilege) != set(privileges):
                raise RuntimeError('Existing export role differs; refusing to overwrite it')
            evidence['export_permissions'] = []
            for entity in [vm, *vm.datastore]:
                previous = [permission for permission in manager.RetrieveEntityPermissions(entity, False)
                            if permission.principal == 'coriolis-reader' and not permission.group]
                evidence['export_permissions'].append({
                    'entity_id': entity._moId, 'entity_name': entity.name,
                    'previous': [{'role_id': permission.roleId, 'propagate': permission.propagate}
                                 for permission in previous]})
            save()
            role_id = roles[0].roleId if roles else manager.AddAuthorizationRole(role_name, privileges)
            evidence['export_role_id'] = role_id
            evidence['export_role_created'] = not bool(roles)
            save()
            for entity in [vm, *vm.datastore]:
                manager.SetEntityPermissions(entity, [vim.AuthorizationManager.Permission(
                    principal='coriolis-reader', group=False, roleId=role_id, propagate=False)])
        if args.assign_instance_uuid or args.enable_cbt or args.snapshot_test:
            if vm.snapshot is not None:
                raise RuntimeError('Existing snapshots require inspection; refusing preparation')
            if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
                raise RuntimeError('Pilot must already be running; no implicit power operation')
        if args.assign_instance_uuid and not vm.config.instanceUuid:
            instance_uuid = str(uuid.uuid4())
            evidence['assigned_instance_uuid'] = instance_uuid
            save()
            task = vm.ReconfigVM_Task(vim.vm.ConfigSpec(instanceUuid=instance_uuid))
            evidence['uuid_task'] = task._moId
            save()
            WaitForTask(task, maxWaitTime=120)
            if vm.config.instanceUuid != instance_uuid:
                raise RuntimeError('API did not retain requested instance UUID')
        if args.enable_cbt and not vm.config.changeTrackingEnabled:
            task = vm.ReconfigVM_Task(vim.vm.ConfigSpec(changeTrackingEnabled=True))
            evidence['cbt_task'] = task._moId
            save()
            WaitForTask(task, maxWaitTime=120)
        if args.snapshot_test:
            if vm.guest.toolsRunningStatus != 'guestToolsRunning':
                raise RuntimeError('Quiesced test requires running VMware Tools')
            name = 'scf-relocate-readiness-' + str(uuid.uuid4())
            evidence['snapshot_name'] = name
            save()
            task = vm.CreateSnapshot_Task(name=name, description='Owned SCF pilot API/CBT acceptance',
                                          memory=False, quiesce=True)
            evidence['snapshot_task'] = task._moId
            save()
            WaitForTask(task, maxWaitTime=180)
            snapshot = task.info.result
            evidence['snapshot_id'] = snapshot._moId
            save()
            try:
                evidence['snapshot_quiesced'] = next(
                    entry.quiesced for entry in vm.snapshot.rootSnapshotList
                    if entry.snapshot == snapshot)
                evidence['cbt_queries'] = []
                for disk in snapshot.config.hardware.device:
                    if isinstance(disk, vim.vm.device.VirtualDisk):
                        changed = vm.QueryChangedDiskAreas(snapshot, disk.key, 0, '*')
                        evidence['cbt_queries'].append({
                            'disk_key': disk.key, 'change_id': getattr(disk.backing, 'changeId', None),
                            'length': changed.length, 'changed_areas': len(changed.changedArea)})
                save()
            finally:
                task = snapshot.RemoveSnapshot_Task(removeChildren=False, consolidate=True)
                evidence['snapshot_removal_task'] = task._moId
                save()
                WaitForTask(task, maxWaitTime=180)
                evidence['snapshot_removed'] = True
                save()
        evidence['after'] = inspect_vm(vm)
        if evidence['after']['bios_uuid'] != evidence['before']['bios_uuid']:
            raise RuntimeError('BIOS UUID changed unexpectedly')
        if args.snapshot_test and vm.snapshot is not None:
            raise RuntimeError('Snapshot remained; inspect before continuing')
        evidence['result'] = 'PASS'
        save()
        print(json.dumps(evidence, indent=2))
    finally:
        Disconnect(connection)


if __name__ == '__main__':
    main()