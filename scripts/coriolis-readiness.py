#!/usr/bin/env python3
"""Read-only Coriolis inventory. Tokens and connection credentials are never saved."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import yaml

parser = argparse.ArgumentParser()
parser.add_argument('--url', required=True)
parser.add_argument('--credentials-file', required=True)
parser.add_argument('--project', default='admin')
parser.add_argument('--output-dir', default='.local/coriolis')
args = parser.parse_args()
base = args.url.rstrip('/')
parsed = urllib.parse.urlsplit(base)
if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.path:
    parser.error('--url must be an HTTPS origin without credentials or a path')
credentials = yaml.safe_load(Path(args.credentials_file).read_text())
out = Path(args.output_dir)
out.mkdir(parents=True, exist_ok=True, mode=0o700)


def call(path, body=None, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['X-Auth-Token'] = token
    if body is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(base + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response), response.headers


def save(name, value):
    (out / name).write_text(json.dumps(value, indent=2) + '\n')


try:
    config, _ = call('/api/config')
    identity = config['config']['servicesUrls']['keystone']
    api = config['config']['servicesUrls']['coriolis']
    # Use only relative service paths on the explicitly selected trusted origin.
    assert identity.startswith('/') and not identity.startswith('//')
    assert api.startswith('/') and not api.startswith('//')
    _, headers = call(identity + '/auth/tokens', {'auth': {
        'identity': {'methods': ['password'], 'password': {'user': {
            'name': credentials['user'], 'password': credentials['password'],
            'domain': {'name': config['config']['defaultUserDomain']}}}},
        'scope': 'unscoped'}})
    token = headers['X-Subject-Token']
    projects, _ = call(identity + '/auth/projects', token=token)
    matches = [p for p in projects['projects'] if p['name'] == args.project]
    if len(matches) != 1:
        raise SystemExit('Requested project was not found uniquely; stopping')
    project = matches[0]
    _, headers = call(identity + '/auth/tokens', {'auth': {
        'identity': {'methods': ['token'], 'token': {'id': token}},
        'scope': {'project': {'id': project['id']}}}})
    token = headers['X-Subject-Token']
    prefix = api + '/' + project['id']
    inventory = {'project': project['name'], 'project_id': project['id']}
    for resource in ['providers', 'regions', 'services', 'endpoints', 'transfers', 'deployments']:
        data, _ = call(prefix + '/' + resource, token=token)
        value = data[resource]
        if resource in ('endpoints', 'transfers', 'deployments'):
            allowed = {'id', 'name', 'type', 'provider', 'status', 'last_execution_status'}
            value = [{k: v for k, v in item.items() if k in allowed} for item in value]
        inventory[resource] = value
    save('inventory.json', inventory)
    for provider, kinds in [('vmware_vsphere', [16, 8]), ('stackit', [16, 4])]:
        for kind in kinds:
            schema, _ = call(prefix + f'/providers/{provider}/schemas/{kind}', token=token)
            save(f'{provider}-schema-{kind}.json', schema)
    print(json.dumps({
        'project': project['name'], 'providers': list(inventory['providers']),
        'workers': [{k: s.get(k) for k in ['host', 'enabled', 'status']}
                    for s in inventory['services']],
        'endpoints': len(inventory['endpoints']), 'transfers': len(inventory['transfers']),
        'deployments': len(inventory['deployments']), 'output_dir': str(out)}, indent=2))
except urllib.error.HTTPError as error:
    raise SystemExit(f'Coriolis returned HTTP {error.code}; no response body or credentials logged')
