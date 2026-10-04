import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('coriolis_pilot', root / 'scripts/coriolis-pilot.py')
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
evidence_spec = importlib.util.spec_from_file_location('workload_evidence', root / 'scripts/workload-evidence.py')
evidence = importlib.util.module_from_spec(evidence_spec)
evidence_spec.loader.exec_module(evidence)
remote_spec = importlib.util.spec_from_file_location('lab_remote', root / 'scripts/lab-remote.py')
remote = importlib.util.module_from_spec(remote_spec)
remote_spec.loader.exec_module(remote)
network_spec = importlib.util.spec_from_file_location('pilot_network', root / 'scripts/pilot-network.py')
network_module = importlib.util.module_from_spec(network_spec)
network_spec.loader.exec_module(network_module)

os_evidence_spec = importlib.util.spec_from_file_location('os_evidence', root / 'scripts/os-morphing-evidence.py')
os_evidence = importlib.util.module_from_spec(os_evidence_spec)
os_evidence_spec.loader.exec_module(os_evidence)

hardening_spec = importlib.util.spec_from_file_location('hardening', root / 'scripts/harden-target.py')
hardening = importlib.util.module_from_spec(hardening_spec)
hardening_spec.loader.exec_module(hardening)

agent_spec = importlib.util.spec_from_file_location('agent_acceptance', root / 'scripts/agent-acceptance.py')
agent_acceptance = importlib.util.module_from_spec(agent_spec)
agent_spec.loader.exec_module(agent_acceptance)

shutdown_spec = importlib.util.spec_from_file_location('source_shutdown', root / 'scripts/shutdown-source.py')
source_shutdown = importlib.util.module_from_spec(shutdown_spec)
shutdown_spec.loader.exec_module(source_shutdown)


class SourceShutdownGateTests(unittest.TestCase):
    def fixture(self):
        boundary = {'records': 1014, 'seed_records': 1000, 'writes': 14, 'digest': 'digest', 'data_uuid': 'data'}
        owner = {'target_project_id': 'project', 'executions': ['delta'], 'delta_executions': ['delta'],
                 'deployments': ['deployment'], 'rehearsal_executions': {'delta': 'deployment'}}
        acceptance = {'result': 'PASS', 'project_id': 'project', 'deployment_id': 'deployment', 'boundary': boundary}
        frozen = dict(boundary, phase='FROZEN', result='PASS',
                      confirmed_source_uuid='564d168b-e4be-aa72-da5c-6dea41d74b30',
                      source_dmi_uuid='8b164d56-bee4-72aa-da5c-6dea41d74b30')
        return owner, acceptance, frozen

    def test_exact_accepted_and_frozen_source_passes(self):
        source_shutdown.validate_evidence(*self.fixture(), '564d168b-e4be-aa72-da5c-6dea41d74b30')

    def test_shutdown_rejects_unaccepted_or_wrong_delta(self):
        for changes in ({'result': 'pending'}, {'deployment_id': 'foreign'}, {'project_id': 'foreign'}):
            with self.subTest(changes=changes):
                owner, acceptance, frozen = self.fixture()
                acceptance.update(changes)
                with self.assertRaises(ValueError):
                    source_shutdown.validate_evidence(owner, acceptance, frozen, frozen['confirmed_source_uuid'])

    def test_shutdown_rejects_missing_freeze_or_changed_data(self):
        for changes in ({'phase': 'running'}, {'digest': 'changed'}, {'source_dmi_uuid': '00000000-0000-0000-0000-000000000000'}):
            with self.subTest(changes=changes):
                owner, acceptance, frozen = self.fixture()
                frozen.update(changes)
                with self.assertRaises(ValueError):
                    source_shutdown.validate_evidence(owner, acceptance, frozen, frozen['confirmed_source_uuid'])


class FinalCopyGateTests(unittest.TestCase):
    def fixture(self, directory):
        path = Path(directory)
        boundary = {'records': 1014, 'seed_records': 1000, 'writes': 14, 'digest': 'digest', 'data_uuid': 'data'}
        acceptance = {'result': 'PASS', 'project_id': 'project', 'deployment_id': 'deployment', 'boundary': boundary}
        frozen = dict(boundary, result='PASS', phase='FROZEN',
                      confirmed_source_uuid='564d168b-e4be-aa72-da5c-6dea41d74b30',
                      source_dmi_uuid='8b164d56-bee4-72aa-da5c-6dea41d74b30')
        args = argparse.Namespace(state=str(path / 'state.json'), source_endpoint='source',
                                  acceptance_evidence=str(path / 'acceptance.json'),
                                  freeze_evidence=str(path / 'freeze.json'), esxi_host='verified-host',
                                  confirm_source_uuid=frozen['confirmed_source_uuid'])
        state = {'coriolis_project_id': 'scope', 'source_endpoint': 'source', 'instance_id': 'instance',
                 'target_project_id': 'project', 'resources': {'transfer': 'transfer', 'target_endpoint': 'target'},
                 'executions': ['delta'], 'delta_executions': ['delta'], 'deployments': ['deployment'],
                 'rehearsal_executions': {'delta': 'deployment'}}
        pilot.save(args.state, state)
        pilot.save(args.acceptance_evidence, acceptance)
        pilot.save(args.freeze_evidence, frozen)
        return args, state

    def test_final_copy_requires_source_off_before_coriolis_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.fixture(directory)
            client = mock.Mock(project_id='scope')
            with mock.patch.object(pilot, 'verified_source_power_state', return_value='poweredOn'):
                with self.assertRaises(ValueError):
                    pilot.final_transfer(client, args)
            client.api.assert_not_called()

    def test_final_copy_retains_off_source_and_disables_auto_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.fixture(directory)
            client = mock.Mock(project_id='scope')
            client.api.side_effect = [
                {'transfer': {'origin_endpoint_id': 'source', 'destination_endpoint_id': 'target',
                              'instances': ['instance'], 'last_execution_status': 'COMPLETED'}},
                {'execution': {'id': 'delta', 'status': 'COMPLETED'}},
                {'execution': {'id': 'final'}}]
            with mock.patch.object(pilot, 'verified_source_power_state', return_value='poweredOff'):
                pilot.final_transfer(client, args)
            self.assertEqual(client.api.call_args_list[-1], mock.call('/transfers/transfer/executions', {
                'execution': {'shutdown_instances': False, 'auto_deploy': False}}))
            self.assertEqual(json.loads(Path(args.state).read_text())['final_execution'], 'final')
            client.reset_mock()
            with self.assertRaises(ValueError):
                pilot.final_transfer(client, args)
            client.api.assert_not_called()

    def test_final_deployment_blocks_running_source_before_api_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            args, state = self.fixture(directory)
            state.update(final_execution='final', executions=['delta', 'final'],
                         final_frozen_boundary=json.loads(Path(args.freeze_evidence).read_text()))
            pilot.save(args.state, state)
            client = mock.Mock(project_id='scope')
            with mock.patch.object(pilot, 'verified_source_power_state', return_value='poweredOn'):
                with self.assertRaises(ValueError):
                    pilot.deploy_final(client, args)
            client.api.assert_not_called()

class DeploymentEvidenceTests(unittest.TestCase):
    def fixture(self, directory):
        args = argparse.Namespace(state=str(Path(directory) / 'state.json'), source_endpoint='source',
                                  deployment_id='deployment', output=str(Path(directory) / 'evidence.json'))
        pilot.save(args.state, {'coriolis_project_id': 'scope', 'source_endpoint': 'source',
                               'deployments': ['deployment'], 'resources': {'transfer': 'transfer'},
                               'instance_id': 'instance'})
        deployment = {'id': 'deployment', 'transfer_id': 'transfer', 'last_execution_status': 'COMPLETED',
                      'info': {'instance': {'private_evidence': 'not-for-stdout'}}}
        client = mock.Mock(project_id='scope')
        client.api.return_value = {'deployment': deployment}
        return args, client, deployment

    def test_exports_full_private_evidence_with_sanitized_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            args, client, deployment = self.fixture(directory)
            summary = pilot.deployment_evidence(client, args)
            self.assertEqual(json.loads(Path(args.output).read_text()), deployment)
            self.assertEqual(Path(args.output).stat().st_mode & 0o777, 0o600)
            self.assertNotIn('not-for-stdout', json.dumps(summary))
            client.api.assert_called_once_with('/deployments/deployment?include_info=true&include_task_info=true')

    def test_foreign_deployment_rejected_before_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            args, client, _ = self.fixture(directory)
            args.deployment_id = 'foreign'
            with self.assertRaises(ValueError):
                pilot.deployment_evidence(client, args)
            client.api.assert_not_called()

    def test_rejects_running_or_mismatched_deployment_without_export(self):
        for changes in ({'last_execution_status': 'RUNNING'}, {'transfer_id': 'foreign'},
                        {'id': 'foreign'}, {'info': {}}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                args, client, deployment = self.fixture(directory)
                deployment.update(changes)
                with self.assertRaises(ValueError):
                    pilot.deployment_evidence(client, args)
                self.assertFalse(Path(args.output).exists())


class AgentAcceptanceTests(unittest.TestCase):
    def test_parser_accepts_only_the_known_ssh_unit_reload_warning(self):
        output = ''.join('SCF_JSON:' + name + '\n{}\n' for name in ('before', 'hardening', 'after', 'data'))
        warning = ("Warning: The unit file, source configuration file or drop-ins of ssh.service changed "
                   "on disk. Run 'systemctl daemon-reload' to reload units.")
        transport = output.replace('SCF_JSON:hardening\n', 'SCF_JSON:hardening\n' + warning + '\n')
        self.assertEqual(set(agent_acceptance.parse_output(transport)), {'before', 'hardening', 'after', 'data'})
        with self.assertRaises(ValueError):
            agent_acceptance.parse_output(transport.replace(warning, 'unexpected output'))

    def test_compact_audit_keeps_required_acceptance_data(self):
        audit = {'accounts': ['ubuntu'], 'ssh_policy': {'passwordauthentication': 'no'},
                 'block_devices': {'blockdevices': ['vda', 'vdb']}, 'fstab': ['data UUID mount'],
                 'services': {'app': 'active'}, 'mounts': {'pseudo_filesystems': 'large'},
                 'ipv4_addresses': ['verbose address data']}
        compact = agent_acceptance.compact_audit(audit)
        for key in ('accounts', 'ssh_policy', 'block_devices', 'fstab', 'services'):
            self.assertEqual(compact[key], audit[key])
        self.assertNotIn('mounts', compact)
        self.assertNotIn('ipv4_addresses', compact)

    def test_compressed_agent_output_preserves_complete_evidence(self):
        value = {'accounts': [{'user': 'ubuntu', 'uid': 1000}], 'result': 'PASS'}
        payload = agent_acceptance.base64.b64encode(
            agent_acceptance.gzip.compress(json.dumps(value).encode())).decode()
        envelope = json.dumps({'encoding': 'gzip+base64', 'payload': payload})
        output = ''.join('SCF_JSON:' + name + '\n' + envelope + '\n'
                         for name in ('before', 'hardening', 'after', 'data'))
        parsed = agent_acceptance.parse_output(output)
        self.assertEqual(parsed['before'], value)
        self.assertEqual(parsed['after'], value)

    def test_packed_payload_preserves_the_reviewed_script(self):
        script = agent_acceptance.build_script('target-id', '8.8.8.8', {'records': 1014})
        packed = agent_acceptance.packed_script(script)
        payload = agent_acceptance.shlex.split(packed)[2]
        unpacked = agent_acceptance.gzip.decompress(agent_acceptance.base64.b64decode(payload)).decode()
        self.assertEqual(unpacked, script)
        self.assertLess(len(packed), len(script))

    def test_parser_requires_all_unique_json_stages(self):
        output = ''.join('SCF_JSON:' + name + '\n{}\n' for name in ('before', 'hardening', 'after', 'data'))
        self.assertEqual(set(agent_acceptance.parse_output(output)), {'before', 'hardening', 'after', 'data'})
        for broken in (output + 'SCF_JSON:data\n{}\n', 'unexpected\n' + output,
                       'SCF_JSON:data\n{}\n'):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                agent_acceptance.parse_output(broken)

    def test_bundle_only_compares_data_and_uses_exact_target(self):
        script = agent_acceptance.build_script('target-id', '8.8.8.8', {'records': 1014})
        self.assertIn('--expected-server-id target-id', script)
        self.assertIn('--expected-json', script)
        invocation = next(line for line in script.splitlines() if line.startswith('python3 - compare'))
        self.assertIn('--expected-server-id target-id', invocation)
        self.assertNotIn('python3 - write', script)
        self.assertNotIn('python3 - freeze', script)

    def test_validation_rejects_wrong_guest_before_os_acceptance(self):
        boundary = {'records': 1014, 'seed_records': 1000, 'writes': 14, 'digest': 'digest', 'data_uuid': 'data'}
        with self.assertRaises(ValueError):
            agent_acceptance.validate({'data': dict(boundary, result='PASS', server_id='foreign')},
                                      {'server_id': 'target', 'boundary': boundary})


class TargetHardeningTests(unittest.TestCase):
    def test_reference_unit_treats_java_sigterm_as_success(self):
        unit = (root / 'scripts/relocate-demo.service').read_text()
        self.assertIn('SuccessExitStatus=143\n', unit)

    def test_rollback_removes_new_policy_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'drop-in/policy.conf'
            alias = Path(directory) / 'vmtoolsd.service'
            with mock.patch.object(hardening.subprocess, 'run'), \
                    mock.patch.object(hardening, 'policy', return_value={'passwordauthentication': 'yes'}):
                with self.assertRaises(RuntimeError):
                    hardening.apply_policies({path: '[Service]\nSuccessExitStatus=143\n'}, alias, '8.8.8.8')
            self.assertFalse(path.parent.exists())

    def test_socket_activation_runtime_is_created_by_service_not_mkdir(self):
        missing = mock.Mock(returncode=255, stderr='Missing privilege separation directory: /run/sshd\n')
        with mock.patch.object(hardening.subprocess, 'run', side_effect=[missing, mock.Mock(), mock.Mock(), mock.Mock()]) as calls:
            hardening.prepare_ssh_runtime()
        self.assertEqual(calls.call_args_list[1], mock.call(['systemctl', 'daemon-reload'], check=True))
        self.assertEqual(calls.call_args_list[2], mock.call(['systemctl', 'start', 'ssh.service'], check=True))

    def test_other_ssh_error_does_not_start_service(self):
        broken = mock.Mock(returncode=255, stderr='Bad configuration option\n')
        with mock.patch.object(hardening.subprocess, 'run', return_value=broken) as calls:
            with self.assertRaises(RuntimeError):
                hardening.prepare_ssh_runtime()
        self.assertEqual(calls.call_count, 1)

    def test_removes_only_the_proven_dangling_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / 'vmtoolsd.service'
            alias.symlink_to('/usr/lib/systemd/system/open-vm-tools.service')
            self.assertEqual(hardening.stale_alias(alias), '/usr/lib/systemd/system/open-vm-tools.service')
            alias.unlink()
            alias.symlink_to('/some/other.service')
            with self.assertRaises(RuntimeError):
                hardening.stale_alias(alias)

    def test_does_not_overwrite_foreign_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.conf'
            path.write_text('existing policy\n')
            with self.assertRaises(RuntimeError):
                hardening.owned_file(path, 'PasswordAuthentication no\n')

    def test_ssh_policy_failure_rolls_back_created_files_and_does_not_remove_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.conf'
            alias = Path(directory) / 'vmtoolsd.service'
            alias.symlink_to('/usr/lib/systemd/system/open-vm-tools.service')
            with mock.patch.object(hardening.subprocess, 'run'), \
                    mock.patch.object(hardening, 'policy', return_value={'passwordauthentication': 'yes'}):
                with self.assertRaises(RuntimeError):
                    hardening.apply_policies({path: 'PasswordAuthentication no\n'}, alias, '8.8.8.8')
            self.assertFalse(path.exists())
            self.assertTrue(alias.is_symlink())

    def test_kvm_is_required_before_metadata_access(self):
        with mock.patch.object(hardening.subprocess, 'check_output', return_value='vmware\n'), \
                mock.patch.object(hardening.urllib.request, 'build_opener') as opener:
            with self.assertRaises(RuntimeError):
                hardening.verify_target('9c4d1839-e4eb-4b88-bfbe-af8a57061d2f')
            opener.assert_not_called()

    def test_target_metadata_must_match(self):
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(hardening.subprocess, 'check_output', return_value='kvm\n'), \
                mock.patch.object(hardening.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(hardening.json, 'load', return_value={'uuid': 'foreign'}):
            with self.assertRaises(RuntimeError):
                hardening.verify_target('9c4d1839-e4eb-4b88-bfbe-af8a57061d2f')


class OsMorphingEvidenceTests(unittest.TestCase):
    def test_authorized_keys_only_returns_fingerprints(self):
        encoded = os_evidence.base64.b64encode(b'public-key-material').decode()
        keys = f'# ignored\nssh-ed25519 {encoded} private-comment\nrestrict ssh-ed25519 {encoded} other-comment'
        fingerprints = os_evidence.key_fingerprints(keys)
        self.assertEqual(len(fingerprints), 1)
        self.assertTrue(fingerprints[0].startswith('SHA256:'))
        self.assertNotIn('comment', str(fingerprints))
        self.assertNotIn(encoded, str(fingerprints))

    def test_bad_key_is_ignored(self):
        self.assertEqual(os_evidence.key_fingerprints('ssh-ed25519 invalid! comment'), [])

    def test_driver_follows_parent_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'virtio_blk').mkdir()
            (path / 'device/block').mkdir(parents=True)
            (path / 'device/driver').symlink_to(path / 'virtio_blk')
            self.assertEqual(os_evidence.driver(path / 'device/block'), 'virtio_blk')


class SourceMutationIdentityTests(unittest.TestCase):
    source_uuid = '564d168b-e4be-aa72-da5c-6dea41d74b30'

    def test_confirmed_vmware_source_is_accepted(self):
        with mock.patch.object(evidence.subprocess, 'check_output', return_value='vmware\n'), \
                mock.patch.object(evidence.Path, 'read_text', return_value=self.source_uuid.upper()):
            evidence.verify_source(self.source_uuid)

    def test_same_uuid_on_kvm_is_rejected(self):
        with mock.patch.object(evidence.subprocess, 'check_output', return_value='kvm\n'), \
                mock.patch.object(evidence.Path, 'read_text', return_value=self.source_uuid):
            with self.assertRaises(RuntimeError):
                evidence.verify_source(self.source_uuid)

    def test_linux_smbios_byte_order_is_accepted(self):
        with mock.patch.object(evidence.subprocess, 'check_output', return_value='vmware\n'), \
                mock.patch.object(evidence.Path, 'read_text', return_value='8b164d56-bee4-72aa-da5c-6dea41d74b30'):
            evidence.verify_source(self.source_uuid)

    def test_other_vmware_guest_is_rejected(self):
        with mock.patch.object(evidence.subprocess, 'check_output', return_value='vmware\n'), \
                mock.patch.object(evidence.Path, 'read_text', return_value=self.source_uuid):
            with self.assertRaises(RuntimeError):
                evidence.verify_source('00000000-0000-0000-0000-000000000000')

    def test_write_rejects_changed_boundary_before_first_post(self):
        expected = {'records': 1004, 'seed_records': 1000, 'writes': 4,
                    'digest': 'approved', 'data_uuid': 'data'}
        changed = dict(expected, records=1005)
        with mock.patch('sys.argv', ['evidence', 'write', '--expected-source-uuid', self.source_uuid,
                                     '--expected-json', json.dumps(expected)]), \
                mock.patch.object(evidence.os, 'geteuid', return_value=0), \
                mock.patch.object(evidence.socket, 'gethostname', return_value='scf-relocate-app'), \
                mock.patch.object(evidence, 'verify_source'), \
                mock.patch.object(evidence, 'collect', return_value=changed), \
                mock.patch.object(evidence, 'api') as request:
            with self.assertRaises(RuntimeError):
                evidence.main()
            request.assert_not_called()

    def test_freeze_rejects_same_count_with_changed_digest_before_database_stop(self):
        boundary = {'records': 1014, 'digest': 'approved'}
        with mock.patch('sys.argv', ['evidence', 'freeze', '--expected-source-uuid', self.source_uuid]), \
                mock.patch.object(evidence.os, 'geteuid', return_value=0), \
                mock.patch.object(evidence.socket, 'gethostname', return_value='scf-relocate-app'), \
                mock.patch.object(evidence, 'verify_source', return_value=self.source_uuid), \
                mock.patch.object(evidence, 'collect', return_value=boundary), \
                mock.patch.object(evidence, 'sql', side_effect=['1014', 'changed']), \
                mock.patch.object(evidence.subprocess, 'run') as commands:
            with self.assertRaises(RuntimeError):
                evidence.main()
            self.assertFalse(any(call.args[0][0] == 'pg_ctlcluster' for call in commands.call_args_list))


class PilotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.network = {
            'project_id': 'project', 'carrier_nic_attached': True,
            'resources': {'network': 'network', 'security_group': 'group'}}
        self.network_file = self.path / 'network.json'
        self.network_file.write_text(json.dumps(self.network))
        self.args = argparse.Namespace(
            network_state=str(self.network_file), confirm_project_id='project',
            source_endpoint='source', instance_id='instance', worker_image='image',
            state=str(self.path / 'state.json'), dry_run=True)
        self.client = mock.Mock(project_id='coriolis-project')
        self.client.api.return_value = {'schemas': {
            'source_environment_schema': {'type': 'object'},
            'destination_environment_schema': {'type': 'object'}}}
        self.inventory = mock.patch.object(pilot, 'inventory', return_value={
            'source_connection': True,
            'source_instances': [{'name': 'scf-relocate-app', 'id': 'instance'}]})
        self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def test_rejects_insecure_or_credentialed_origins_before_network_access(self):
        for url in ['http://example.org', 'https://user:secret@example.org',
                    'https://example.org/path', 'https://example.org?token=secret']:
            with self.subTest(url=url), mock.patch.object(pilot.urllib.request, 'urlopen') as request:
                with self.assertRaises(ValueError):
                    pilot.CoriolisClient(url, 'unused')
                request.assert_not_called()

    def test_rejects_cross_origin_request(self):
        client = object.__new__(pilot.CoriolisClient)
        with self.assertRaises(ValueError):
            client.request('//example.org/path')

    def test_wrong_project_stops_before_api_calls(self):
        self.args.confirm_project_id = 'other-project'
        with self.assertRaises(ValueError):
            pilot.prepare_transfer(self.client, self.args)
        self.client.api.assert_not_called()

    def test_pending_network_stops_before_api_calls(self):
        self.network['pending_operation'] = 'create_nic'
        self.network_file.write_text(json.dumps(self.network))
        with self.assertRaises(ValueError):
            pilot.prepare_transfer(self.client, self.args)
        self.client.api.assert_not_called()

    def test_wrong_instance_stops_before_schema_calls(self):
        self.args.instance_id = 'probe-not-app'
        with self.assertRaises(ValueError):
            pilot.prepare_transfer(self.client, self.args)
        self.client.api.assert_not_called()

    def test_dry_run_uses_private_workers_and_validates_without_creation(self):
        result = pilot.create_transfer(self.client, self.args)
        target = result['definition']['destination_environment']
        self.assertFalse(target['migr_worker_use_public_ip'])
        self.assertFalse(target['use_public_ip'])
        self.assertFalse(result['definition']['source_environment']['skip_nfc_validation'])
        self.assertTrue(result['definition']['source_environment']['verify_disk_integrity'])
        self.assertEqual(result['definition']['scenario'], 'live_migration')
        self.assertTrue(result['definition']['clone_disks'])
        self.assertFalse(Path(self.args.state).exists())
        self.assertTrue(all(len(call.args) == 1 for call in self.client.api.call_args_list))

    def state(self, **changes):
        state = {'coriolis_project_id': 'coriolis-project', 'source_endpoint': 'source',
                 'resources': {'transfer': 'owned-transfer'}, 'executions': [], 'deployments': []}
        state.update(changes)
        Path(self.args.state).write_text(json.dumps(state))

    def test_initial_copy_cannot_be_duplicated(self):
        self.state(executions=['existing-execution'])
        with self.assertRaises(ValueError):
            pilot.execute_transfer(self.client, self.args)
        self.client.api.assert_not_called()

    def test_pending_operation_prevents_execution(self):
        self.state(pending_operation='uncertain-create')
        with self.assertRaises(ValueError):
            pilot.execute_transfer(self.client, self.args)
        self.client.api.assert_not_called()

    def test_rehearsal_requires_completed_transfer(self):
        self.state(executions=['existing'])
        self.client.api.return_value = {'transfer': {'last_execution_status': 'ERROR'}}
        with self.assertRaises(ValueError):
            pilot.deploy_rehearsal(self.client, self.args)
        self.assertEqual(len(self.client.api.call_args_list), 1)

    def test_initial_copy_never_requests_shutdown_or_auto_deploy(self):
        self.state()
        self.client.api.return_value = {'execution': {'id': 'execution'}}
        pilot.execute_transfer(self.client, self.args)
        self.client.api.assert_called_once_with('/transfers/owned-transfer/executions', {
            'execution': {'shutdown_instances': False, 'auto_deploy': False}})
        self.assertEqual(json.loads(Path(self.args.state).read_text())['executions'], ['execution'])


    def retry_fixture(self, **changes):
        self.state(executions=['failed'], instance_id='instance',
                   resources={'transfer': 'owned-transfer', 'target_endpoint': 'owned-target'})
        transfer = {'origin_endpoint_id': 'source', 'destination_endpoint_id': 'owned-target',
                    'instances': ['instance'], 'last_execution_status': 'ERROR'}
        execution = {'id': 'failed', 'status': 'ERROR', 'tasks': [
            {'task_type': name, 'status': 'COMPLETED'} for name in [
                'DELETE_TRANSFER_SOURCE_RESOURCES', 'DELETE_TRANSFER_TARGET_RESOURCES']]}
        transfer.update(changes)
        self.client.api.side_effect = [{'transfer': transfer}, {'execution': execution},
                                       {'execution': {'id': 'retry'}}]
        return execution

    def test_retry_preserves_source_and_existing_transfer(self):
        self.retry_fixture()
        pilot.retry_transfer(self.client, self.args)
        self.assertEqual(self.client.api.call_args_list[-1], mock.call(
            '/transfers/owned-transfer/executions', {
                'execution': {'shutdown_instances': False, 'auto_deploy': False}}))
        self.assertEqual(json.loads(Path(self.args.state).read_text())['executions'], ['failed', 'retry'])

    def test_retry_refuses_running_or_foreign_transfer(self):
        for changes in [{'last_execution_status': 'RUNNING'}, {'origin_endpoint_id': 'foreign'},
                        {'destination_endpoint_id': 'foreign'}, {'instances': ['other']}]:
            with self.subTest(changes=changes):
                self.client.reset_mock()
                self.retry_fixture(**changes)
                with self.assertRaises(ValueError):
                    pilot.retry_transfer(self.client, self.args)
                self.assertEqual(len(self.client.api.call_args_list), 1)

    def test_retry_refuses_incomplete_cleanup(self):
        execution = self.retry_fixture()
        execution['tasks'][0]['status'] = 'ERROR'
        with self.assertRaises(ValueError):
            pilot.retry_transfer(self.client, self.args)
        self.assertEqual(len(self.client.api.call_args_list), 2)

    def test_retry_refuses_unjournaled_or_pending_execution(self):
        for changes in [{'executions': []}, {'pending_operation': 'uncertain-retry'},
                        {'deployments': ['existing-target']}]:
            with self.subTest(changes=changes):
                self.client.reset_mock()
                self.state(**changes)
                with self.assertRaises(ValueError):
                    pilot.retry_transfer(self.client, self.args)
                self.client.api.assert_not_called()


    def sync_fixture(self, **changes):
        execution = self.retry_fixture(last_execution_status='COMPLETED', **changes)
        execution['status'] = 'COMPLETED'

    def test_delta_preserves_source_and_reuses_transfer(self):
        self.sync_fixture()
        pilot.sync_transfer(self.client, self.args)
        self.assertEqual(self.client.api.call_args_list[-1], mock.call(
            '/transfers/owned-transfer/executions', {
                'execution': {'shutdown_instances': False, 'auto_deploy': False}}))
        self.assertEqual(json.loads(Path(self.args.state).read_text())['executions'], ['failed', 'retry'])
        self.assertEqual(json.loads(Path(self.args.state).read_text())['delta_executions'], ['retry'])

    def test_delta_refuses_foreign_transfer(self):
        self.sync_fixture(instances=['other'])
        with self.assertRaises(ValueError):
            pilot.sync_transfer(self.client, self.args)
        self.assertEqual(len(self.client.api.call_args_list), 1)

    def test_delta_refuses_failed_owned_execution(self):
        self.retry_fixture(last_execution_status='COMPLETED')
        with self.assertRaises(ValueError):
            pilot.sync_transfer(self.client, self.args)
        self.assertEqual(len(self.client.api.call_args_list), 2)

    def test_delta_refuses_pending_operation(self):
        self.state(executions=['completed'], pending_operation='uncertain-sync')
        with self.assertRaises(ValueError):
            pilot.sync_transfer(self.client, self.args)
        self.client.api.assert_not_called()


    def test_rehearsal_rejects_foreign_transfer(self):
        self.retry_fixture(last_execution_status='COMPLETED', origin_endpoint_id='foreign')
        with self.assertRaises(ValueError):
            pilot.deploy_rehearsal(self.client, self.args)
        self.assertEqual(len(self.client.api.call_args_list), 1)

    def test_rehearsal_clones_only_owned_transfer_without_force(self):
        self.retry_fixture()
        self.client.api.side_effect = [
            {'transfer': {'origin_endpoint_id': 'source', 'destination_endpoint_id': 'owned-target',
                          'instances': ['instance'], 'last_execution_status': 'COMPLETED'}},
            {'deployment': {'id': 'rehearsal'}}]
        pilot.deploy_rehearsal(self.client, self.args)
        self.assertEqual(self.client.api.call_args_list[-1], mock.call('/deployments', {'deployment': {
            'transfer_id': 'owned-transfer', 'clone_disks': True, 'force': False,
            'skip_os_morphing': False}}))
        self.assertEqual(json.loads(Path(self.args.state).read_text())['deployments'], ['rehearsal'])


    def test_status_reads_deployment_tasks_without_private_details(self):
        self.state(resources={}, deployments=['rehearsal'])
        self.client.api.return_value = {'deployment': {
            'id': 'rehearsal', 'last_execution_status': 'RUNNING', 'tasks': [{
                'id': 'task', 'task_type': 'OS_MORPHING', 'status': 'RUNNING',
                'exception_details': 'private-details'}]}}
        result = pilot.status(self.client, self.args)
        self.assertEqual(result['deployments'][0]['tasks'], [{
            'id': 'task', 'task_type': 'OS_MORPHING', 'status': 'RUNNING'}])
        self.assertNotIn('private-details', json.dumps(result))

    def delta_rehearsal_fixture(self):
        self.state(executions=['copy', 'delta'], delta_executions=['delta'], deployments=['original'],
                   instance_id='instance',
                   resources={'transfer': 'owned-transfer', 'target_endpoint': 'owned-target'})
        execution = {'id': 'delta', 'status': 'COMPLETED', 'tasks': [
            {'task_type': 'DELETE_TRANSFER_SOURCE_RESOURCES', 'status': 'COMPLETED'},
            {'task_type': 'DELETE_TRANSFER_TARGET_RESOURCES', 'status': 'COMPLETED'}]}
        previous = {'id': 'original', 'transfer_id': 'owned-transfer', 'last_execution_status': 'COMPLETED'}
        self.client.api.side_effect = [
            {'transfer': {'origin_endpoint_id': 'source', 'destination_endpoint_id': 'owned-target',
                          'instances': ['instance'], 'last_execution_status': 'COMPLETED'}},
            {'execution': execution}, {'deployment': previous}, {'deployment': {'id': 'fresh'}}]
        return execution, previous

    def test_delta_rehearsal_clones_once_without_replacing_original(self):
        self.delta_rehearsal_fixture()
        pilot.deploy_delta_rehearsal(self.client, self.args)
        self.assertEqual(self.client.api.call_args_list[-1], mock.call('/deployments', {'deployment': {
            'transfer_id': 'owned-transfer', 'clone_disks': True, 'force': False, 'skip_os_morphing': False}}))
        state = json.loads(Path(self.args.state).read_text())
        self.assertEqual(state['deployments'], ['original', 'fresh'])
        self.assertEqual(state['rehearsal_executions'], {'delta': 'fresh'})
        self.client.reset_mock()
        with self.assertRaises(ValueError):
            pilot.deploy_delta_rehearsal(self.client, self.args)
        self.client.api.assert_not_called()

    def test_delta_rehearsal_requires_journaled_delta_and_no_pending_operation(self):
        for changes in [{'delta_executions': []}, {'pending_operation': 'uncertain-deployment'}]:
            with self.subTest(changes=changes):
                self.delta_rehearsal_fixture()
                state = json.loads(Path(self.args.state).read_text())
                state.update(changes)
                pilot.save(self.args.state, state)
                self.client.reset_mock()
                with self.assertRaises(ValueError):
                    pilot.deploy_delta_rehearsal(self.client, self.args)
                self.client.api.assert_not_called()

    def test_delta_rehearsal_requires_completed_cleanup(self):
        execution, _ = self.delta_rehearsal_fixture()
        execution['tasks'][0]['status'] = 'ERROR'
        with self.assertRaises(ValueError):
            pilot.deploy_delta_rehearsal(self.client, self.args)
        self.assertEqual(len(self.client.api.call_args_list), 2)

    def test_delta_rehearsal_rejects_foreign_or_incomplete_previous_deployment(self):
        for changes in [{'transfer_id': 'foreign'}, {'id': 'foreign'}, {'last_execution_status': 'RUNNING'}]:
            with self.subTest(changes=changes):
                _, previous = self.delta_rehearsal_fixture()
                previous.update(changes)
                with self.assertRaises(ValueError):
                    pilot.deploy_delta_rehearsal(self.client, self.args)
                self.assertEqual(len(self.client.api.call_args_list), 3)
                self.client.reset_mock()


class PublicTargetTests(unittest.TestCase):
    def test_only_approved_ssh_ingress_and_group_internal_traffic_are_allowed(self):
        rules = [{'direction': 'egress'}, {'direction': 'ingress', 'remoteSecurityGroupId': 'group'},
                 {'direction': 'ingress', 'ethertype': 'IPv4', 'ipRange': '8.8.8.8/32',
                  'protocol': {'name': 'tcp'}, 'portRange': {'min': 22, 'max': 22}}]
        network_module.check_public_ingress(rules, {'8.8.8.8/32'}, 'group')

    def test_worldwide_or_non_ssh_ingress_is_rejected(self):
        for source, port in [('0.0.0.0/0', 22), ('8.8.8.8/32', 5432)]:
            with self.subTest(source=source, port=port), self.assertRaises(ValueError):
                network_module.check_public_ingress([{
                    'direction': 'ingress', 'ethertype': 'IPv4', 'ipRange': source,
                    'protocol': {'name': 'tcp'}, 'portRange': {'min': port, 'max': port}}],
                    {'8.8.8.8/32'}, 'group')

    def plan_fixture(self):
        network = {'project_id': 'project', 'resources': {'network': 'network', 'security_group': 'group'}}
        pilot = {'target_project_id': 'project', 'deployments': ['deployment'],
                 'resources': {'transfer': 'transfer'}, 'instance_id': 'source'}
        deployment = {'id': 'deployment', 'last_execution_status': 'COMPLETED',
                      'transfer_id': 'transfer', 'info': {'source': {'instance_deployment_info': {
                          'instance_name': 'app', 'nic_ids': ['nic'], 'volumes_info': [{'volume_id': 'volume'}]}}}}
        server = {'id': 'server', 'name': 'app', 'status': 'ACTIVE', 'volumes': ['volume'],
                  'nics': [{'nicId': 'nic', 'networkId': 'network', 'nicSecurity': True,
                            'securityGroups': ['group']}]}
        return network, pilot, deployment, server

    def test_public_target_requires_owned_matching_resources(self):
        fixture = self.plan_fixture()
        plan = network_module.public_target_plan(*fixture, '8.8.8.8/32')
        self.assertEqual(plan['nic_id'], 'nic')
        fixture[3]['volumes'] = ['foreign-volume']
        with self.assertRaises(ValueError):
            network_module.public_target_plan(*fixture, '8.8.8.8/32')

    def test_public_target_refuses_broad_or_private_source(self):
        for source in ['0.0.0.0/0', '8.8.8.0/24', '10.0.0.1/32']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                network_module.public_target_plan(*self.plan_fixture(), source)


class RemoteArgumentTests(unittest.TestCase):
    def test_forwards_guest_flags_without_parsing_them_as_ssh_flags(self):
        parsed = remote.argument_parser().parse_args([
            'destination', '--carrier-host', 'carrier', '--destination-host', 'target',
            '--destination-known-hosts', 'verified-hosts', '--script', 'evidence.py',
            '--script-args', 'compare', '--expected-json', '{"records":1004}',
            '--expected-server-id', 'approved-server'])
        self.assertEqual(parsed.script_args, [
            'compare', '--expected-json', '{"records":1004}', '--expected-server-id', 'approved-server'])
        self.assertEqual(parsed.destination_known_hosts, 'verified-hosts')
        self.assertEqual(parsed.destination_host, 'target')

    def test_preserves_existing_positional_script_arguments(self):
        parsed = remote.argument_parser().parse_args([
            'source', '--carrier-host', 'carrier', '--script', 'evidence.py', '--script-args', 'compare'])
        self.assertEqual(parsed.script_args, ['compare'])


class WorkloadEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.boundary = {'records': 1004, 'seed_records': 1000, 'writes': 4,
                         'digest': 'approved-digest', 'data_uuid': 'approved-filesystem'}

    def test_exact_boundary_is_accepted_without_mutation(self):
        result = dict(self.boundary)
        evidence.compare_expected(result, self.boundary)
        self.assertEqual(result, self.boundary)

    def test_every_boundary_mismatch_is_rejected(self):
        for key in self.boundary:
            with self.subTest(key=key):
                result = {**self.boundary, key: 'different'}
                with self.assertRaisesRegex(RuntimeError, key):
                    evidence.compare_expected(result, self.boundary)

    def test_cloud_server_identity_must_match(self):
        with mock.patch.object(evidence.urllib.request, 'urlopen'), mock.patch.object(
                evidence.json, 'load', return_value={'uuid': 'owned-destination'}):
            self.assertEqual(evidence.verify_server('owned-destination'), 'owned-destination')
            with self.assertRaises(RuntimeError):
                evidence.verify_server('wrong-server')

    def test_absent_cloud_server_identity_is_rejected(self):
        with mock.patch.object(evidence.urllib.request, 'urlopen'), mock.patch.object(
                evidence.json, 'load', return_value={}):
            with self.assertRaises(RuntimeError):
                evidence.verify_server('owned-destination')


if __name__ == '__main__':
    unittest.main()