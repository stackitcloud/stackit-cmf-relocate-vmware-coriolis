#!/usr/bin/env python3
"""Compare authenticated NFC service identities without disk reads or library patches."""
import argparse
import json
from pathlib import Path
import ssl

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl
from pyVmomi.VmomiSupport import CreateManagedType, GetVmodlType


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--username', default='coriolis-reader')
    parser.add_argument('--password-file', default='.local/esxi/coriolis-reader-password')
    args = parser.parse_args()
    try:
        service_type = GetVmodlType('vim.NfcService')
    except KeyError:
        CreateManagedType('vim.NfcService', 'NfcService', 'vmodl.ManagedObject',
                          'vim.version.version1', [], [
                              ('getVmFiles', 'NfcGetVmFiles', 'vim.version.version1',
                               (('vm', 'vim.VirtualMachine', 'vim.version.version1', 0, None),),
                               (0, 'vim.HostServiceTicket', 'vim.HostServiceTicket'), None, None)])
        service_type = GetVmodlType('vim.NfcService')
    connection = SmartConnect(host=args.host, user=args.username,
                              pwd=Path(args.password_file).read_text(), sslContext=ssl.create_default_context())
    try:
        content = connection.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            matches = [vm for vm in view.view if vm.name == 'scf-relocate-app']
        finally:
            view.Destroy()
        if len(matches) != 1:
            raise RuntimeError('Exact pilot VM must resolve uniquely')
        result = {'api_type': content.about.apiType, 'username': args.username, 'services': []}
        for service_id in ['nfcService', 'ha-nfc-service']:
            record = {'service_id': service_id}
            try:
                ticket = service_type(service_id, connection._stub).GetVmFiles(matches[0])
                record.update(result='TICKET_RECEIVED', ticket_host=ticket.host,
                              ticket_port=ticket.port, service=ticket.service)
            except vim.fault.NoPermission as error:
                record.update(result='NO_PERMISSION', privilege_id=error.privilegeId)
            except vmodl.fault.ManagedObjectNotFound:
                record.update(result='MANAGED_OBJECT_NOT_FOUND')
            result['services'].append(record)
        print(json.dumps(result, indent=2))
    finally:
        Disconnect(connection)


if __name__ == '__main__':
    main()