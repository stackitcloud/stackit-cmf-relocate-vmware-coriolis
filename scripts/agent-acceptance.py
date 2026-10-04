#!/usr/bin/env python3
"""Journal one exact deployment's hardening and read-only guest acceptance via its Agent."""
import argparse
import base64
import gzip
import io
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parent
pilot_spec = importlib.util.spec_from_file_location('pilot', ROOT / 'coriolis-pilot.py')
pilot = importlib.util.module_from_spec(pilot_spec)
pilot_spec.loader.exec_module(pilot)
network_spec = importlib.util.spec_from_file_location('network', ROOT / 'pilot-network.py')
network = importlib.util.module_from_spec(network_spec)
network_spec.loader.exec_module(network)


AUDIT_KEYS = ('captured_at', 'hypervisor', 'kernel', 'efi_boot', 'machine_id_sha256',
              'accounts', 'services', 'ssh_policy', 'ssh_host_key_fingerprints',
              'vmtools_unit_links', 'failed_units', 'packages', 'network_drivers',
              'block_drivers', 'pci_drivers', 'block_devices', 'fstab', 'ipv4_routes',
              'dns', 'cloud_init', 'relevant_loaded_modules', 'sudo_policy_sha256')


def compact_audit(value):
    result = {key: value[key] for key in AUDIT_KEYS if key in value}
    if 'cloud_init' in result:
        status = json.loads(result['cloud_init']['output'])
        result['cloud_init'] = {'exit_code': result['cloud_init']['exit_code'],
                                **{key: status.get(key) for key in
                                   ('status', 'extended_status', 'datasource', 'errors', 'recoverable_errors')}}
    return result


def build_script(server_id, source, boundary):
    stages = [
        ('before', 'os-morphing-evidence.py', []),
        ('hardening', 'harden-target.py', ['--expected-server-id', server_id,
                                         '--ssh-source', source, '--apply']),
        ('after', 'os-morphing-evidence.py', []),
        ('data', 'workload-evidence.py', ['compare', '--expected-json', json.dumps(boundary),
                                        '--expected-server-id', server_id]),
    ]
    script = '#!/bin/sh\nset -eu\n'
    for stage, filename, arguments in stages:
        content = (ROOT / filename).read_text()
        delimiter = 'SCF_' + stage.upper() + '_SCRIPT'
        if delimiter in content.splitlines():
            raise ValueError('Unexpected script delimiter collision')
        script += f"printf 'SCF_JSON:{stage}\\n'\n"
        script += 'python3 - ' + shlex.join(arguments) + f" <<'{delimiter}'\n"
        script += 'import base64, contextlib, gzip, io, json\n'
        script += 'buffer = io.StringIO()\nwith contextlib.redirect_stdout(buffer):\n'
        script += '    exec(compile(' + repr(content) + ', ' + repr(filename)
        script += ", 'exec'), {'__name__': '__main__', '__file__': " + repr(filename) + '})\n'
        script += 'value = json.loads(buffer.getvalue())\n'
        if stage in ('before', 'after'):
            script += 'value = {key: value[key] for key in ' + repr(AUDIT_KEYS) + ' if key in value}\n'
            script += "status = json.loads(value['cloud_init']['output'])\n"
            script += "value['cloud_init'] = {'exit_code': value['cloud_init']['exit_code'], **{key: status.get(key) for key in ('status', 'extended_status', 'datasource', 'errors', 'recoverable_errors')}}\n"
        script += "payload = base64.b64encode(gzip.compress(json.dumps(value).encode(), mtime=0)).decode()\n"
        script += "print(json.dumps({'encoding': 'gzip+base64', 'payload': payload}))\n"
        script += delimiter + '\n'
    return script


def packed_script(script):
    payload = base64.b64encode(gzip.compress(script.encode(), mtime=0)).decode()
    return 'printf %s ' + shlex.quote(payload) + ' | base64 --decode | gzip --decompress | sh\n'


def parse_output(output):
    parts = output.split('SCF_JSON:')
    if parts[0].strip():
        raise ValueError('Unexpected output before acceptance evidence')
    result = {}
    for part in parts[1:]:
        name, body = part.split('\n', 1)
        if name not in ('before', 'hardening', 'after', 'data') or name in result:
            raise ValueError('Unexpected or duplicate acceptance stage')
        known_warning = ("Warning: The unit file, source configuration file or drop-ins of ssh.service changed "
                         "on disk. Run 'systemctl daemon-reload' to reload units.")
        body = '\n'.join(line for line in body.splitlines() if line != known_warning)
        value = json.loads(body)
        if value.get('encoding') == 'gzip+base64':
            compressed = base64.b64decode(value['payload'], validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                decoded = stream.read(1_000_001)
            if len(decoded) > 1_000_000:
                raise ValueError('Acceptance stage exceeds the evidence size limit')
            value = json.loads(decoded)
        result[name] = value
    if set(result) != {'before', 'hardening', 'after', 'data'}:
        raise ValueError('Incomplete acceptance evidence')
    return result


def validate(evidence, record):
    data = evidence['data']
    pilot_boundary = record['boundary']
    for key in ('records', 'seed_records', 'writes', 'digest', 'data_uuid'):
        if data.get(key) != pilot_boundary[key]:
            raise ValueError('Target differs from approved boundary: ' + key)
    if data.get('result') != 'PASS' or data.get('server_id') != record['server_id']:
        raise ValueError('Target data identity/result does not match approval')
    after = evidence['after']
    if (after['ssh_policy'].get('passwordauthentication') != 'no'
            or after['ssh_policy'].get('pubkeyauthentication') != 'yes'
            or after['accounts'] != evidence['before']['accounts']
            or after['accounts'] != record['source_accounts']
            or after['vmtools_unit_links'] or after['failed_units']):
        raise ValueError('Target OS/account/SSH hardening acceptance failed')
    if (after['hypervisor'] != 'kvm' or not after['efi_boot']
            or after['network_drivers'].get('eth0') != 'virtio_net'
            or any(after['block_drivers'].get(name) != 'virtio_blk' for name in ('vda', 'vdb'))):
        raise ValueError('Expected KVM/EFI/Virtio target is not present')
    for service in ('relocate-demo.service', 'postgresql@16-relocate.service',
                    'stackit-server-agent.service', 'stackit-server-monitoring-agent.service'):
        if after['services'][service]['ActiveState'] != 'active':
            raise ValueError('Required target service is not active: ' + service)
    if evidence['hardening'].get('result') != 'PASS':
        raise ValueError('Target hardening did not pass')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'resume', 'status'])
    parser.add_argument('--target-server')
    parser.add_argument('--ssh-source')
    parser.add_argument('--deployment-evidence')
    parser.add_argument('--boundary', default='.local/pilot/delta-boundary.json')
    parser.add_argument('--source-audit', default='.local/pilot/os-morphing-source.json')
    parser.add_argument('--state', default='.local/pilot/coriolis.json')
    parser.add_argument('--network-state', default='.local/pilot/network.json')
    parser.add_argument('--record', default='.local/pilot/delta-acceptance-agent.json')
    parser.add_argument('--prior-record')
    parser.add_argument('--cli', default='.local/tools/stackit')
    parser.add_argument('--credentials', default='~/.ssh/cf-migration-sa.json')
    args = parser.parse_args()
    os.umask(0o077)
    for value in (args.record, args.state, args.network_state, args.boundary, args.source_audit,
                  args.deployment_evidence, args.prior_record):
        if value and not Path(value).resolve().is_relative_to(Path('.local').resolve()):
            parser.error('Journals and evidence must remain under ignored .local')
    journal = Path(args.record)
    if args.action == 'start' and journal.exists():
        parser.error('Acceptance journal already exists; inspect status, do not repeat submission')
    network_state = json.loads(Path(args.network_state).read_text())
    auth = subprocess.run([args.cli, 'auth', 'activate-service-account', '--service-account-key-path',
                           str(Path(args.credentials).expanduser()), '--only-print-access-token'],
                          capture_output=True, text=True, check=True, timeout=90)
    environment = dict(os.environ, STACKIT_ACCESS_TOKEN=auth.stdout.strip())

    def cloud(*command):
        result = subprocess.run([args.cli, *command, '--project-id', network_state['project_id'],
                                 '--region', network_state['region'], '--output-format', 'json',
                                 '--assume-yes'], env=environment, capture_output=True, text=True, timeout=180)
        if result.returncode:
            pilot.save(journal.with_suffix('.error.json'), {
                'operation': list(command[:3]), 'exit_code': result.returncode, 'stderr': result.stderr})
            raise RuntimeError('Scoped STACKIT operation failed; response and credentials not logged')
        return json.loads(result.stdout)

    if args.action in ('start', 'resume'):
        if not args.target_server or not args.ssh_source or not args.deployment_evidence:
            parser.error('Start requires explicit target, SSH source and owned deployment evidence')
        owner = json.loads(Path(args.state).read_text())
        deployment = json.loads(Path(args.deployment_evidence).read_text())
        server = cloud('server', 'describe', args.target_server)
        plan = network.public_target_plan(network_state, owner, deployment, server, args.ssh_source + '/32')
        if not server.get('agent', {}).get('provisioned'):
            raise ValueError('Exact target Server Agent must already be provisioned')
        boundary = json.loads(Path(args.boundary).read_text())
        source_accounts = json.loads(Path(args.source_audit).read_text())['accounts']
        record = {'server_id': plan['server_id'], 'deployment_id': plan['deployment_id'],
                  'project_id': network_state['project_id'], 'boundary': boundary,
                  'source_accounts': source_accounts, 'pending_operation': 'submit_acceptance'}
        if args.prior_record:
            prior = json.loads(Path(args.prior_record).read_text())
            for key in ('server_id', 'deployment_id', 'project_id', 'boundary', 'source_accounts'):
                if prior.get(key) != record[key]:
                    raise ValueError('Retry must match the same approved target and evidence')
            failed = cloud('server', 'command', 'describe', str(prior['command_id']),
                           '--server-id', record['server_id'])
            if failed.get('status') != 'failed':
                raise ValueError('Prior command must be terminally failed; never retry an active/unknown job')
            record['prior_command_id'] = prior['command_id']
        script_path = journal.with_suffix('.sh')
        script = build_script(record['server_id'], args.ssh_source, boundary)
        if args.action == 'resume':
            previous = json.loads(journal.read_text())
            if previous != record or previous.get('command_id'):
                raise ValueError('Resume requires exactly the same unsubmitted acceptance journal')
            commands = cloud('server', 'command', 'list', '--server-id', record['server_id'])
            if isinstance(commands, dict):
                if 'items' in commands:
                    commands = commands['items']
                elif 'commands' in commands:
                    commands = commands['commands']
                else:
                    raise ValueError('Unknown command-list format; refusing reconciliation')
            if not isinstance(commands, list) or commands:
                raise ValueError('Unknown or existing target commands require manual reconciliation')
            if script_path.read_text() != script:
                raise ValueError('Original failed payload differs; refusing replacement')
            source_path = journal.with_suffix('.source.sh')
            with source_path.open('x') as stream:
                stream.write(script)
            script_path.write_text(packed_script(script))
        else:
            pilot.save(journal, record)
            with script_path.open('x') as stream:
                stream.write(packed_script(script))
        command = cloud('server', 'command', 'create', '--server-id', record['server_id'],
                        '--template-name', 'RunShellScript', '--params', 'script=@{' + str(script_path.resolve()) + '}')
        record['command_id'] = command['id']
        record.pop('pending_operation')
        pilot.save(journal, record)
        print(json.dumps({'server_id': record['server_id'], 'command_id': record['command_id'],
                          'result': 'ACCEPTANCE_SUBMITTED'}, indent=2))
        return
    record = json.loads(journal.read_text())
    if record.get('pending_operation') or record['project_id'] != network_state['project_id']:
        raise ValueError('Reconcile pending acceptance or project scope before reading results')
    command = cloud('server', 'command', 'describe', str(record['command_id']), '--server-id', record['server_id'])
    pilot.save(journal.with_suffix('.result.json'), command)
    summary = {'server_id': record['server_id'], 'command_id': record['command_id'], 'status': command['status']}
    if command['status'] == 'completed':
        if command.get('exitCode') != 0:
            raise ValueError('Target acceptance command failed; inspect private evidence, do not resubmit')
        evidence = parse_output(command['output'])
        validate(evidence, record)
        pilot.save(journal.with_suffix('.evidence.json'), evidence)
        record['result'] = 'PASS'
        pilot.save(journal, record)
        summary.update(result='PASS', records=evidence['data']['records'], digest=evidence['data']['digest'],
                       ssh_password_authentication=evidence['after']['ssh_policy']['passwordauthentication'])
    elif command['status'] == 'failed':
        record['result'] = 'FAILED'
        pilot.save(journal, record)
        summary.update(result='FAILED', exit_code=command.get('exitCode'))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()