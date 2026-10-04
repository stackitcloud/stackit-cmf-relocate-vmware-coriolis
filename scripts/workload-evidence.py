#!/usr/bin/env python3
"""Compare SQL/API data without writes, or freeze the explicitly approved lab source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import urllib.request
import uuid


DIGEST_SQL = "SELECT md5(string_agg(record_id || ':' || payload, E'\\n' ORDER BY record_id)) FROM migration_records"


def api(path, method='GET'):
    request = urllib.request.Request('http://127.0.0.1:8080' + path, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def sql(query):
    return subprocess.check_output(
        ['runuser', '-u', 'postgres', '--', 'psql', '-d', 'relocate', '-At',
         '-v', 'ON_ERROR_STOP=1', '-c', query], text=True).strip()


def collect():
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'relocate-writer.timer']).returncode == 0:
        raise RuntimeError('Stop the writer timer before recording a fixed data boundary')
    if api('/actuator/health')['status'] != 'UP':
        raise RuntimeError('Application is not healthy')
    result = api('/api/evidence')
    expected_seed = hashlib.md5('\n'.join(
        f'seed-{number:06d}:' + hashlib.md5(f'scf-relocate-{number}'.encode()).hexdigest()
        for number in range(1, 1001)).encode()).hexdigest()
    if sql(DIGEST_SQL + " WHERE kind='seed'") != expected_seed or result['seed_records'] != 1000:
        raise RuntimeError('Independent seed verification failed')
    if int(sql('SELECT count(*) FROM migration_records')) != result['records'] or sql(DIGEST_SQL) != result['digest']:
        raise RuntimeError('SQL and API data evidence disagree')
    if sql('SHOW data_directory') != '/srv/relocate-data/postgresql':
        raise RuntimeError('PostgreSQL data is not on the expected separate mount')
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/srv/relocate-data'], text=True).strip()
    result.update(data_uuid=mount, seed_digest=expected_seed, result='PASS', hostname=socket.gethostname())
    return result


def compare_expected(result, expected):
    for key in ['records', 'seed_records', 'writes', 'digest', 'data_uuid']:
        if result[key] != expected[key]:
            raise RuntimeError('Destination differs from the approved data boundary: ' + key)


def verify_server(server_id):
    with urllib.request.urlopen('http://169.254.169.254/openstack/latest/meta_data.json', timeout=5) as response:
        metadata = json.load(response)
    if metadata.get('uuid') != server_id:
        raise RuntimeError('Guest metadata does not match the approved destination server')
    return metadata['uuid']


def verify_source(source_uuid):
    expected = uuid.UUID(source_uuid)
    hypervisor = subprocess.check_output(['systemd-detect-virt'], text=True).strip()
    actual = uuid.UUID(Path('/sys/class/dmi/id/product_uuid').read_text().strip())
    if hypervisor != 'vmware' or actual not in (expected, uuid.UUID(bytes_le=expected.bytes)):
        raise RuntimeError('Source mutation requires the explicitly confirmed VMware guest UUID')
    return str(actual)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['compare', 'write', 'freeze'])
    boundary = parser.add_mutually_exclusive_group()
    boundary.add_argument('--expected')
    boundary.add_argument('--expected-json')
    parser.add_argument('--expected-server-id')
    parser.add_argument('--expected-source-uuid')
    parser.add_argument('--writes', type=int, default=10)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with approved passwordless sudo on the dedicated guest')
    if args.expected_server_id and args.action != 'compare':
        parser.error('Destination identity verification applies only to read-only comparison')
    if args.expected_source_uuid and args.action == 'compare':
        parser.error('Source mutation identity applies only to write or freeze')
    if args.action in ('write', 'freeze') and socket.gethostname() != 'scf-relocate-app':
        parser.error('Source mutation is restricted to the dedicated lab guest')
    if args.action in ('write', 'freeze'):
        if not args.expected_source_uuid:
            parser.error('Source mutation requires --expected-source-uuid')
        source_dmi_uuid = verify_source(args.expected_source_uuid)
    if args.action == 'write':
        if not 1 <= args.writes <= 100:
            parser.error('Synthetic write batch must be between 1 and 100')
        before = collect()
        if args.expected or args.expected_json:
            expected = json.loads(Path(args.expected).read_text() if args.expected else args.expected_json)
            compare_expected(before, expected)
        for number in range(args.writes):
            api('/api/writes', method='POST')
        after = collect()
        if after['records'] != before['records'] + args.writes:
            raise RuntimeError('Synthetic write count did not match the requested batch')
        print(json.dumps({'result': 'PASS', 'before': before, 'after': after}, indent=2))
        return
    if args.action == 'freeze':
        subprocess.run(['systemctl', 'stop', 'relocate-writer.timer', 'relocate-writer.service'], check=True)
    result = collect()
    if args.expected or args.expected_json:
        expected = json.loads(Path(args.expected).read_text() if args.expected else args.expected_json)
        compare_expected(result, expected)
    if args.expected_server_id:
        result['server_id'] = verify_server(args.expected_server_id)
    if args.action == 'freeze':
        subprocess.run(['systemctl', 'stop', 'relocate-demo'], check=True)
        if (int(sql('SELECT count(*) FROM migration_records')) != result['records']
                or sql(DIGEST_SQL) != result['digest']):
            raise RuntimeError('Data changed while stopping the application')
        subprocess.run(['pg_ctlcluster', '--mode', 'fast', '16', 'relocate', 'stop'], check=True)
        subprocess.run(['sync'], check=True)
        result['phase'] = 'FROZEN'
        result['confirmed_source_uuid'] = str(uuid.UUID(args.expected_source_uuid))
        result['source_dmi_uuid'] = source_dmi_uuid
        directory = Path('/var/lib/scf-relocate')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(directory / 'freeze.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()