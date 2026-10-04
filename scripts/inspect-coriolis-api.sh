#!/bin/bash
set -euo pipefail
timeout 30 docker exec -i coriolis-worker python3 - <<'PY'
import ast
import importlib.util
import json
from pathlib import Path

root = Path(importlib.util.find_spec('coriolis').origin).parent
paths = [root / 'api/v1/transfers.py', root / 'api/v1/deployments.py',
         root / 'api/v1/transfer_actions.py']
paths.extend(sorted((root / 'api/v1').glob('*execution*.py')))
for path in paths:
    if not path.exists():
        continue
    tree = ast.parse(path.read_text())
    methods = []
    for method in ast.walk(tree):
        if not isinstance(method, ast.FunctionDef) or method.name not in (
                'create', '_execute', '_validate_create_body', '_validate_deployment_input', '_deploy'):
            continue
        fields = set()
        calls = []
        for node in ast.walk(method):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and node.args:
                    if isinstance(node.args[0], ast.Constant):
                        fields.add(str(node.args[0].value))
                if isinstance(node.func, ast.Attribute) and any(
                        marker in node.func.attr for marker in ('create', 'execute', 'deploy')):
                    calls.append(ast.unparse(node))
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                fields.add(str(node.slice.value))
        methods.append({'method': method.name, 'fields': sorted(fields), 'calls': calls})
    print(json.dumps({'file': str(path.relative_to(root)), 'methods': methods}))
provider_spec = importlib.util.find_spec('coriolis_provider_stackit')
if provider_spec is not None:
    provider = Path(provider_spec.origin).parent
    for filename in ['common.py', 'imp.py']:
        path = provider / filename
        if not path.exists():
            continue
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if any(marker in line for marker in ('0.0.0.0/0', '5566', 'source_ip', 'allowed_ips')):
                print(filename, index + 1, '\n'.join(lines[max(0, index - 2):index + 3]))
PY