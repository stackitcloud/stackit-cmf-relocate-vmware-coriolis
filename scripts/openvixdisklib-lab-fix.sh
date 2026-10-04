#!/bin/bash
set -euo pipefail
action=${1:?Usage: openvixdisklib-lab-fix.sh self-test|check|apply|rollback}
if [[ "$action" = self-test ]]; then
    runner=(python3 - "$action")
else
    [[ $EUID -eq 0 ]]
    runner=(timeout 30 docker exec -i coriolis-worker python3 - "$action")
fi
"${runner[@]}" <<'PY'
import argparse
import ast
import __future__
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace


def transform(source):
    service = '    return nfc_cls(NFC_SERVICE_MOID, si._stub)'
    updated_service = '''    service_id = (
        "ha-nfc-service" if si.RetrieveContent().about.apiType == "HostAgent"
        else NFC_SERVICE_MOID
    )
    return nfc_cls(service_id, si._stub)'''
    ticket = '        return nfc.GetVmFiles(vm)'
    updated_ticket = '''        ticket = nfc.GetVmFiles(vm)
        if not ticket.host and si.RetrieveContent().about.apiType == "HostAgent":
            from urllib.parse import urlsplit
            ticket.host = urlsplit("//" + si._stub.host).hostname
            if not ticket.host:
                raise ValueError("Standalone NFC ticket has no usable endpoint host")
        return ticket'''
    if source.count(service) != 1 or source.count(ticket) != 1:
        raise RuntimeError('Installed source differs from the reviewed NFC anchors; refusing modification')
    result = source.replace(service, updated_service).replace(ticket, updated_ticket)
    compile(result, 'nfc_auth.py', 'exec')
    return result


def test(source):
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('nfc_service', 'get_nfc_ticket')]
    if len(functions) != 2:
        raise RuntimeError('Expected exactly the reviewed NFC service/ticket functions')
    cases = [
        ('HostAgent', None, 'esxi.example:443', 'ha-nfc-service', 'esxi.example'),
        ('HostAgent', None, '[2001:db8::1]:443', 'ha-nfc-service', '2001:db8::1'),
        ('HostAgent', 'advertised.example', 'esxi.example:443', 'ha-nfc-service', 'advertised.example'),
        ('VirtualCenter', 'advertised.example', 'vcenter.example:443', 'nfcService', 'advertised.example'),
        ('VirtualCenter', None, 'vcenter.example:443', 'nfcService', None),
    ]
    for api_type, ticket_host, endpoint, expected_service, expected_host in cases:
        selected = []
        ticket = SimpleNamespace(host=ticket_host)
        service = SimpleNamespace(GetVmFiles=lambda vm: ticket)
        def construct(service_id, stub):
            selected.append(service_id)
            return service
        namespace = {
            '_register_nfc_types': lambda: None, 'GetVmodlType': lambda name: construct,
            'NFC_SERVICE_MOID': 'nfcService'}
        code = compile(ast.Module(body=functions, type_ignores=[]), 'nfc-functions', 'exec',
                       flags=__future__.annotations.compiler_flag)
        exec(code, namespace)
        connection = SimpleNamespace(
            _stub=SimpleNamespace(host=endpoint),
            RetrieveContent=lambda: SimpleNamespace(about=SimpleNamespace(apiType=api_type)))
        result = namespace['get_nfc_ticket'](connection, object())
        if selected != [expected_service] or result.host != expected_host:
            raise RuntimeError('NFC hotfix changed the wrong service or endpoint')
    return len(cases)


parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['self-test', 'check', 'apply', 'rollback'])
args = parser.parse_args()
if args.action == 'self-test':
    fixture = '''def nfc_service(si):
    _register_nfc_types()
    nfc_cls = GetVmodlType('vim.NfcService')
    return nfc_cls(NFC_SERVICE_MOID, si._stub)

def get_nfc_ticket(si, vm, disk_device_key=None, host_for_access=None, read_only=True, disk_path=None):
    nfc = nfc_service(si)
    if read_only and disk_device_key is None and disk_path is None:
        return nfc.GetVmFiles(vm)
    raise RuntimeError('Not exercised by this read-only lab test')
'''
    print(json.dumps({'result': 'PASS', 'cases': test(transform(fixture))}))
    raise SystemExit(0)

module = importlib.util.find_spec('openvixdisklib')
if module is None or not module.origin:
    raise RuntimeError('Expected installed OpenVixDiskLib module was not found')
path = Path(module.origin).parent / 'nfc_auth.py'
backup = path.with_name(path.name + '.pre-scf-lab-fix')
manifest = path.with_name(path.name + '.scf-lab-fix.json')
original = path.read_text()
current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
if args.action == 'rollback':
    record = json.loads(manifest.read_text())
    if current_hash != record['patched_sha256']:
        raise RuntimeError('Library changed after the lab fix; refusing destructive rollback')
    if hashlib.sha256(backup.read_bytes()).hexdigest() != record['original_sha256']:
        raise RuntimeError('Original backup digest differs; refusing rollback')
    temporary = path.with_name(path.name + '.scf-restore')
    temporary.write_bytes(backup.read_bytes())
    temporary.chmod(stat.S_IMODE(path.stat().st_mode))
    temporary.replace(path)
    record['state'] = 'ROLLED_BACK'
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))
    raise SystemExit(0)
if manifest.exists():
    record = json.loads(manifest.read_text())
    if record.get('state') != 'APPLIED' or current_hash != record['patched_sha256']:
        raise RuntimeError('Existing lab-fix journal does not match this library; inspect manually')
    print(json.dumps({'result': 'ALREADY_APPLIED', 'cases': test(original), **record}))
    raise SystemExit(0)
patched = transform(original)
record = {'state': 'CHECKED', 'path': str(path), 'backup': str(backup),
          'original_sha256': current_hash,
          'patched_sha256': hashlib.sha256(patched.encode()).hexdigest(),
          'cases': test(patched), 'scope': 'HostAgent read-only NFC service/ticket compatibility'}
if args.action == 'apply':
    if backup.exists():
        raise RuntimeError('Original backup already exists without a journal; refusing overwrite')
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(path.read_bytes())
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    manifest.chmod(0o600)
    temporary = path.with_name(path.name + '.scf-patched')
    temporary.write_text(patched)
    temporary.chmod(stat.S_IMODE(path.stat().st_mode))
    temporary.replace(path)
    record['state'] = 'APPLIED'
    manifest.write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record))
PY
if [[ "$action" = apply ]]; then
    backup_directory=/var/lib/scf-relocate-lab-fix
    library_directory=/usr/local/lib/python3.10/dist-packages/openvixdisklib
    install -d -m 0700 "$backup_directory"
    backup_name=nfc_auth.py.pre-scf-lab-fix
    if [[ ! -e "$backup_directory/$backup_name" ]]; then
        docker cp "coriolis-worker:$library_directory/$backup_name" "$backup_directory/$backup_name"
    fi
    container_digest=$(docker exec coriolis-worker sha256sum "$library_directory/$backup_name" | cut -d ' ' -f 1)
    host_digest=$(sha256sum "$backup_directory/$backup_name" | cut -d ' ' -f 1)
    [[ "$container_digest" = "$host_digest" ]]
    docker cp "coriolis-worker:$library_directory/nfc_auth.py.scf-lab-fix.json" "$backup_directory/nfc_auth.py.scf-lab-fix.json"
    chmod 0600 "$backup_directory/$backup_name" "$backup_directory/nfc_auth.py.scf-lab-fix.json"
    printf 'Original library backup verified on appliance host: %s\n' "$backup_directory"
fi