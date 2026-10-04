#!/bin/bash
set -euo pipefail
timeout 30 docker exec -i coriolis-worker python3 - <<'PY'
import ast
import importlib.util
from pathlib import Path

provider = Path(importlib.util.find_spec('coriolis_provider_vmware_vsphere').origin).parent
path = provider / 'exp.py'
tree = ast.parse(path.read_text())
for method in ast.walk(tree):
    if isinstance(method, ast.FunctionDef) and any(
            marker in ast.unparse(method).lower() for marker in ('openvixdisklib.', 'nfcservice', 'connectex(')):
        print('METHOD:', method.name)
        print(ast.unparse(method)[:6500])
spec = importlib.util.find_spec('openvixdisklib')
print('OPENVIXDISKLIB MODULE:', spec.origin if spec else 'NOT FOUND')
if spec and spec.origin.endswith('.py'):
    root = Path(spec.origin).parent
    print('MODULE IMPORTS:', '\n'.join(Path(spec.origin).read_text().splitlines()[:35]))
    for path in sorted(root.glob('*.py')):
        tree = ast.parse(path.read_text())
        if path.name == 'nfc_auth.py':
            for line in path.read_text().splitlines():
                if line.startswith('NFC_SERVICE_MOID'):
                    print('SERVICE CONSTANT:', line)
        for method in ast.walk(tree):
            if isinstance(method, ast.FunctionDef) and any(
                    marker in ast.unparse(method).lower() for marker in ('nfcservice', 'hostgetvmfiles', 'acquireticket', 'connectex')) or (
                    isinstance(method, ast.FunctionDef) and path.name == 'nfc_auth.py'
                    and method.name in ('get_nfc_ticket', 'connect_authd')):
                print('NFC METHOD:', path.name, method.name)
                print(ast.unparse(method)[:5000])
print('VDDK WRAPPER:', importlib.util.find_spec('vixDiskLib'))
PY