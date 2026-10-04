#!/usr/bin/env python3
"""Read-only inventory via a local SSH tunnel and explicitly trusted lab certificate."""
import argparse
import json
import ssl
from pathlib import Path
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=18443)
parser.add_argument('--certificate', default='.local/esxi/esxi-cert.pem')
parser.add_argument('--system-trust', action='store_true')
parser.add_argument('--password-file', default='.local/esxi/root-password')
args = parser.parse_args()
context = ssl.create_default_context(cafile=None if args.system_trust else args.certificate)
context.check_hostname = args.host not in ('127.0.0.1', '::1')
if not args.system_trust:
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
si = SmartConnect(host=args.host, port=args.port, user='root',
                  pwd=Path(args.password_file).read_text(), sslContext=context)
try:
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    try:
        hosts = []
        for host in view.view:
            hosts.append({
                'name': host.name, 'connectionState': host.runtime.connectionState,
                'version': host.config.product.version, 'build': host.config.product.build,
                'cpuModel': host.summary.hardware.cpuModel,
                'cpuCores': host.summary.hardware.numCpuCores,
                'memoryBytes': host.summary.hardware.memorySize,
                'nestedHVSupported': host.capability.nestedHVSupported,
                'vmCount': len(host.vm),
                'datastores': [{'name': d.name, 'accessible': d.summary.accessible,
                                'capacity': d.summary.capacity, 'freeSpace': d.summary.freeSpace}
                               for d in host.datastore],
                'nics': [{'device': n.device, 'driver': n.driver, 'linkUp': n.linkSpeed is not None}
                         for n in host.config.network.pnic],
            })
        licenses = [{'name': x.name, 'edition': x.editionKey, 'total': x.total, 'used': x.used}
                    for x in content.licenseManager.licenses]
        print(json.dumps({'apiVersion': content.about.apiVersion, 'hosts': hosts,
                          'licenses': licenses}, indent=2))
    finally:
        view.Destroy()
finally:
    Disconnect(si)
