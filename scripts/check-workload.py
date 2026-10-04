#!/usr/bin/env python3
"""Run on the guest with the bundled writer stopped; validate API against SQL."""
import hashlib
import json
import subprocess
import urllib.request
import uuid


def api(path, method='GET'):
    request = urllib.request.Request('http://127.0.0.1:8080' + path, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def sql(query):
    return subprocess.check_output(
        ['sudo', '-n', '-u', 'postgres', 'psql', '-d', 'relocate', '-At',
         '-v', 'ON_ERROR_STOP=1', '-c', query], text=True).strip()


assert subprocess.run(['systemctl', 'is-active', '--quiet', 'relocate-writer.timer']).returncode != 0, 'Stop writer first'
assert api('/actuator/health')['status'] == 'UP'
before = api('/api/evidence')
assert before['seed_records'] == 1000
expected_seed = hashlib.md5('\n'.join(
    f'seed-{n:06d}:' + hashlib.md5(f'scf-relocate-{n}'.encode()).hexdigest()
    for n in range(1, 1001)).encode()).hexdigest()
actual_seed = sql("SELECT md5(string_agg(record_id || ':' || payload, E'\\n' ORDER BY record_id)) FROM migration_records WHERE kind='seed'")
assert actual_seed == expected_seed, 'Seed content differs'
assert sql('SHOW data_directory') == '/srv/relocate-data/postgresql'
assert int(sql('SELECT count(*) FROM migration_records')) == before['records']
assert sql("SELECT md5(string_agg(record_id || ':' || payload, E'\\n' ORDER BY record_id)) FROM migration_records") == before['digest']
write = api('/api/writes', method='POST')
uuid.UUID(write['record_id'])
after = api('/api/evidence')
assert after['records'] == before['records'] + 1
assert after['writes'] == before['writes'] + 1
assert after['digest'] != before['digest']
assert sql("SELECT count(*) FROM migration_records WHERE record_id='" + write['record_id'] + "'") == '1'
print(json.dumps({'result': 'PASS', 'seed_digest': expected_seed, 'before': before, 'after': after}, indent=2))
