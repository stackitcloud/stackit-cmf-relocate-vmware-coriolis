#!/usr/bin/env python3
"""Run inside the actual Coriolis worker. Checks connectivity only, never VM data."""
import argparse
import json
from pathlib import Path
import socket
import ssl
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--ca-file', required=True, help='Trusted ESXi installer CA PEM')
parser.add_argument('--endpoint-host', required=True, help='Verified ESXi endpoint hostname')
args = parser.parse_args()
result = {}
with urllib.request.urlopen(f'https://{args.endpoint_host}/ui/', timeout=15) as response:
    result['public_https_status'] = response.status
context = ssl.create_default_context(cafile=str(Path(args.ca_file)))
with socket.create_connection(('10.0.2.15', 443), timeout=10) as raw:
    with context.wrap_socket(raw, server_hostname='localhost.localdomain') as tls:
        tls.sendall(b'GET /ui/ HTTP/1.1\r\nHost: localhost.localdomain\r\nConnection: close\r\n\r\n')
        result['private_https_status_line'] = tls.recv(1024).split(b'\r\n')[0].decode()
        result['private_tls_verified'] = True
with socket.create_connection(('10.0.2.15', 902), timeout=10) as nfc:
    result['nfc_902_banner'] = nfc.recv(512).decode().strip()
with socket.create_connection((args.endpoint_host, 902), timeout=10) as nfc:
    result['endpoint_nfc_902_banner'] = nfc.recv(512).decode().strip()
assert 'VMware' in result['endpoint_nfc_902_banner'] and 'NFCSSL' in result['endpoint_nfc_902_banner']
try:
    with socket.create_connection(('10.0.2.15', 22), timeout=3):
        pass
except (TimeoutError, OSError):
    result['private_ssh_blocked'] = True
else:
    raise SystemExit('Unexpected SSH access through restricted tunnel')
assert result['public_https_status'] == 200
assert result['private_https_status_line'].split()[1] == '200'
assert 'VMware' in result['nfc_902_banner'] and 'NFCSSL' in result['nfc_902_banner']
print(json.dumps(result, indent=2))
