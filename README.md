# VMware Relocate to STACKIT with Coriolis

This reference moves one Ubuntu VM containing Spring Boot and self-managed PostgreSQL from
licensed VMware ESXi to STACKIT. The system disk (12 GiB) and PostgreSQL data disk (8 GiB)
remain separate. It is a disk-based Relocate, not an application rebuild or database PaaS migration.

## Scope and prerequisites

- An approved, dedicated STACKIT project, its organization ID, eu01-1 capacity and a service-account key.
- Activate the STACKIT Agent Service once for the destination project before using Server Agent
  commands. This project-level prerequisite is separate from installing/provisioning the agent on
  each server. The operator activated it manually in the retained pilot's destination project.
- A licensed Coriolis appliance with the VMware and STACKIT providers and one healthy worker.
- A legitimately licensed ESXi source that permits API snapshots, CBT and disk export.
- One Ubuntu 24.04 VM named `scf-relocate-app`, with VMware Tools, a system disk and a blank 8-GiB
  data disk. Use the supplied VMX only for the documented isolated lab, not as vendor support evidence.
- A privately reachable source and an existing Linux carrier with pinned SSH host keys.
- Verified source TLS and worker management/NFC reachability. Public browser access alone is insufficient.
- Explicit approval for cloud costs, source changes, rehearsal and final source shutdown.

The retained source lab is nested ESXi on a STACKIT Linux carrier using mandatory KVM.
Its successful tests are not a supported-production or general ESXi-on-STACKIT claim.
The open Coriolis issue with empty standalone instance UUIDs remains a support issue; the pilot
used an explicit API-assigned lab instance UUID without changing its BIOS UUID.

The isolated lab passed initial copy, rehearsal, delta synchronization, controlled source shutdown,
final copy and private target acceptance. This is not a production traffic switch or a vendor support claim.
Keep operational journals and support correspondence private and outside Git.
Do not interpret an in-progress transfer as a completed migration.

## Prepare a reviewed checkout

Use a reviewed checkout containing these scripts. Preserve an existing checkout and its `.local`
directory. Do not reset it, recreate the appliance or infer cleanup from a failed local command.

```sh
umask 077
python3 -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
.local/venv/bin/python -m pip check
.local/venv/bin/python tests/test-pilot.py
```

The tested STACKIT CLI is 0.73.0, downloaded from its official release and verified with the
publisher SHA-256 digest. Supply it at `.local/tools/stackit` or use the helpers' `--cli` option.
The installed Coriolis provider versions, not the OVA filename, control migration behavior.

Keep keys, Coriolis login files, generated application credentials, private host keys, inventories,
state and logs outside Git. `.local/` is ignored. Never use `set -x`, print tokens or paste license keys.

## Source workload

Copy `workload/` and `scripts/` into a private guest working directory over verified SSH. On the
fresh dedicated source guest, execute the following only after checking the blank data disk:

```sh
sudo -n bash /home/ubuntu/scf-workload/scripts/setup-workload.sh /home/ubuntu/scf-workload
sudo -n systemctl stop relocate-writer.timer relocate-writer.service
sudo -n python3 /home/ubuntu/scf-workload/scripts/check-workload.py
```

`setup-workload.sh` refuses another hostname, an incorrect disk size, existing partitions or
unknown filesystem signatures. It mounts the data disk by UUID at `/srv/relocate-data`, installs
PostgreSQL 16 and Java 21, builds the bundled Spring Boot application and creates systemd services.
The database password is generated on the guest. Database and HTTP listeners remain loopback-only.

`check-workload.py` independently compares deterministic seed content, PostgreSQL counts and
digests with the API, then creates one test write. It is deliberately **not** a read-only comparison.
Use `curl -fsS http://127.0.0.1:8080/api/evidence` for a non-mutating baseline and repeat the
independent SQL comparison before accepting a copied destination. Stop the synthetic writer first.

## Licensed source readiness

Record these private operator inputs without printing secrets:

```sh
export ESXI_HOST='your-verified-esxi-hostname'
export CORIOLIS_URL='https://your-verified-coriolis-hostname'
export CORIOLIS_CREDENTIALS='/private/path/coriolis-credentials.var'
export STACKIT_PROJECT_ID='your-approved-project-uuid'
export STACKIT_ORGANIZATION_ID='your-organization-uuid'
export CARRIER_SERVER_ID='your-existing-carrier-server-uuid'
export CARRIER_HOST='your-pinned-carrier-address'
export SOURCE_ENDPOINT_ID='your-owned-vmware-endpoint-uuid'
export SOURCE_INSTANCE_ID='the-verified-application-instance-uuid'
```

Pass `--endpoint-host "$ESXI_HOST"` to `check-esxi-connectivity.py` and set `ESXI_PUBLIC_HOST`
in the Caddy service environment before using `scripts/esxi.Caddyfile`.
For the read-only OS audit, set `SCF_SSH_CLIENT_ADDRESS` on each guest to the intended operator
address when evaluating address-dependent SSH rules. Its default is the documentation address
`192.0.2.1`, not a real operator address. Keep the resulting evidence private.

The Coriolis YAML login file contains `user` and `password`. The source root password file is
`.local/esxi/root-password`, mode 0600; it is only an automation input, never a CLI argument.

```sh
.local/venv/bin/python scripts/esxi-inventory.py \
  --host "$ESXI_HOST" --port 443 --system-trust
.local/venv/bin/python scripts/prepare-source.py \
  --host "$ESXI_HOST" --enable-cbt --snapshot-test \
  --output .local/esxi/source-readiness.json
```

If the documented empty-UUID lab exception is explicitly approved, add `--assign-instance-uuid`
to this first preparation command. It changes only an empty instance UUID on the exact pilot VM.
Do not apply it to arbitrary VMs or describe it as a provider fix.

Use a dedicated VMware account. The retained lab account is `coriolis-reader`; the helper's
`--configure-export-role` grants explicit snapshot/CBT/export privileges on only the pilot VM
and its datastore, records previous direct grants and refuses a differing pre-existing role.
Test the actual account using `--username coriolis-reader --password-file
.local/esxi/coriolis-reader-password --snapshot-test` with a new evidence path.
No helper silently changes account passwords or grants global Administrator access.

```sh
.local/venv/bin/python scripts/coriolis-pilot.py inventory \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
```

Require the correct VM ID and a valid endpoint. A VM missing from inventory is not a transfer failure.
The quiesced-snapshot test establishes VMware API behavior, not final PostgreSQL application consistency.

## Private target and worker path

The current STACKIT provider creates temporary-worker ingress rules for `0.0.0.0/0`. This pilot
therefore uses **no public worker IP** and a separate network reachable through
WireGuard and the existing carrier. Carrier forwarding permits only TCP 22/5566 to that subnet;
SNAT provides a deterministic return path. Targets start private. After separate operator approval,
`pilot-network.py publish-target` can publish only the owned, completed rehearsal target with
SSH limited to one approved global IPv4 `/32`. It does not expose migration workers.

The lab network is `10.77.242.0/24`, carrier `10.77.242.50`. STACKIT reserves `.1` for the gateway
and `.2` for metadata. The source guest remains `10.0.2.20`; no source IP preservation is requested.
The existing management/NFC path and default routes remain unchanged.

```sh
.local/venv/bin/python scripts/pilot-network.py create \
  --project-id "$STACKIT_PROJECT_ID" --carrier-server "$CARRIER_SERVER_ID"
.local/venv/bin/python scripts/pilot-network.py status \
  --project-id "$STACKIT_PROJECT_ID" --carrier-server "$CARRIER_SERVER_ID"
```

Apply `configure-pilot-route.sh carrier <recorded-carrier-MAC>` through pinned carrier SSH using
`lab-remote.py`. Apply `configure-pilot-route.sh appliance` through the existing authenticated
appliance Server Agent. It requires the existing managed WireGuard configuration and saves the
original configuration before the narrow update. `nft -c` validates carrier rules before activation.
Inspect both default routes and repeat source health/digest checks after networking changes.

Every cloud creation is journaled before the request. A `pending_operation` requires reconciliation,
not blind retries. The dedicated `resume` action handles only the proven reserved-metadata-address
NIC failure; it is not a general recovery or adoption mechanism.

## Initial transfer

```sh
.local/venv/bin/python scripts/coriolis-pilot.py create-transfer --dry-run \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID" --instance-id "$SOURCE_INSTANCE_ID" \
  --confirm-project-id "$STACKIT_PROJECT_ID"
.local/venv/bin/python scripts/coriolis-pilot.py create-transfer \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID" --instance-id "$SOURCE_INSTANCE_ID" \
  --confirm-project-id "$STACKIT_PROJECT_ID" --organization-id "$STACKIT_ORGANIZATION_ID"
.local/venv/bin/python scripts/coriolis-pilot.py execute-transfer \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
.local/venv/bin/python scripts/coriolis-pilot.py status \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
```

The default STACKIT key input is `~/.ssh/cf-migration-sa.json`; use `--stackit-key` for another
private key file. It is base64-encoded only in memory for the authenticated endpoint request,
never in the ownership journal. The default official Ubuntu worker image is the one tested in
the retained lab; use `--worker-image` after verifying image availability for another project.

The helper reads live provider schemas and validates exact source/project mapping. It explicitly
selects `live_migration`, openvixdisklib, NFC validation, disk integrity, OS morphing, DHCP,
eu01-1 and c3i.2. Initial execution sets `shutdown_instances: false` and `auto_deploy: false`.
Pre-existing endpoints, transfers and deployments are not adopted, retried or deleted.

## Failed-copy recovery and standalone NFC compatibility

In the retained lab, OpenVixDiskLib initially requested the unavailable `nfcService` object
on standalone ESXi. A same-account countercheck returned an NFC ticket from `ha-nfc-service`,
but the ticket had no advertised host. This issue has been reported to Cloudbase. Prefer a
vendor-supported correction; the temporary helper below is an explicitly approved lab workaround,
not a production prerequisite or proof that disk replication succeeds.

Run the helper's focused tests from this checkout:

```sh
bash scripts/openvixdisklib-lab-fix.sh self-test
```

Only after explicit approval, copy the reviewed helper onto the appliance. As root, from the
directory containing that copy, first check the installed source and then apply:

```sh
bash ./openvixdisklib-lab-fix.sh check
bash ./openvixdisklib-lab-fix.sh apply
```

The helper refuses unexpected source anchors, tests standalone/vCenter selection and missing-host
fallback, and journals hashes before atomically replacing the library. It preserves existing ticket
hosts and does not change TLS/NFC validation. It verifies a private original-file backup on the
appliance host under `/var/lib/scf-relocate-lab-fix`, in addition to the in-container backup.

Confirm there are no active jobs in any accessible Coriolis project before restarting only
`coriolis-worker`. Confirm the worker is running again and the expected library hash is installed.
A replacement container may remove the workaround; never reapply without checking the vendor
version and current source. For rollback, use the helper's `rollback` action while jobs are inactive,
then reload that worker. Rollback refuses later library changes or a mismatched original backup.

After the error is addressed, retry the existing owned transfer rather than running
`create-transfer` or duplicating the initial-copy action:

```sh
.local/venv/bin/python scripts/coriolis-pilot.py retry-transfer \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
```

This requires matching live source/target/VM identities, an ERROR execution, both resource-cleanup
tasks COMPLETED, and no pending operation or existing deployment. It records the new execution
before continuing and never requests source shutdown or automatic deployment. Check status and
data integrity before proceeding; a successful ticket request alone is not a completed copy.

## Isolated rehearsal

After status reports the initial execution COMPLETED:

```sh
.local/venv/bin/python scripts/coriolis-pilot.py deploy-rehearsal \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
```

This uses the installed Deployment API with `clone_disks: true`, `force: false` and OS morphing
enabled. It is not a Replica/DR workflow and does not switch production traffic.
Do not assume that a successful Deployment alone proves boot, data integrity or application health.
Collect the actual cloud server/volume IDs and verified destination host key before connecting.
Compare disks, mount UUID, SQL counts/digest, API evidence and systemd health. Record defects before retrying.

## Read-only OS-morphing acceptance

Run the same reviewed audit on source and target. Establish the target host key independently;
do not reuse the source host-key pin or disable host-key checking. Keep evidence outside Git:

```sh
umask 077
.local/venv/bin/python scripts/lab-remote.py source \
  --carrier-host "$CARRIER_HOST" --script scripts/os-morphing-evidence.py \
  > .local/pilot/os-morphing-source.json
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -i .local/workload/ssh-key ubuntu@"$TARGET_PUBLIC_IP" 'sudo -n python3 -' \
  < scripts/os-morphing-evidence.py > .local/pilot/os-morphing-target.json
```

Set `TARGET_PUBLIC_IP` only to the approved published target. The audit makes no guest changes.
It records active sysfs driver bindings, packages/services, EFI boot, mounts, DHCP/DNS configuration,
cloud-init, all accounts, password lock status, authorized-key fingerprints and SSH policy. It
does not print private keys, password hashes, agent credentials or cloud-init user-data. Sudo-policy
hashes detect file changes but are not a comprehensive effective sudo-policy evaluation.

The October 3 rehearsal confirmed KVM, active `virtio_blk` and `virtio_net`, removed VMware Tools
binaries, installed/running STACKIT Server Agent and monitoring service, preserved accounts/keys,
and healthy application/PostgreSQL. Generic unused VMware modules in the Ubuntu kernel are not
active VMware drivers and do not require deleting supported kernel files.

Record and resolve these acceptance findings deliberately, rather than silently changing the guest:

- `open-vm-tools` retains configuration files and a dangling `vmtoolsd.service` alias.
- Effective SSH password authentication changed from `no` to `yes` through
  `/etc/ssh/sshd_config.d/50-cloud-init.conf`; all audited password states remain unchanged.
  Locked accounts do not substitute for preserving the intended SSH security policy.
- SSH host keys changed, but `machine-id` did not. Assess identity collisions for parallel rehearsals
  and inventory/monitoring registration before deciding whether to retain or regenerate it.
- Guest disks grew from 12/8 GiB to 13/9 GiB; filesystem UUIDs and persistent mount definitions
  were preserved. Record target sizing and cost rather than assuming identical capacities.

The reviewed post-morphing helper restores key-only SSH and removes only the proven dangling
VMware alias. It requires the exact destination metadata UUID and KVM, defaults to a read-only
plan, refuses foreign policy files and rolls back its newly created files on validation failure:

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -i .local/workload/ssh-key ubuntu@"$TARGET_PUBLIC_IP" \
  "sudo -n python3 - --expected-server-id '$TARGET_SERVER_ID' --ssh-source '$OPERATOR_IPV4'" \
  < scripts/harden-target.py
```

Add `--apply` to the remote Python command only after reviewing that plan. The helper installs
`00-scf-relocate-ssh.conf` and a cloud-init `ssh_pwauth: false` override, validates effective SSH
policy and reloads SSH without restarting the guest. Immediately open a fresh verified SSH
connection and repeat the OS audit. Existing accounts, keys and machine identity are not rewritten.
The October 3 target passed this check with unchanged accounts and healthy application/database.

Keep `retain_user_credentials: true`: the installed provider describes it as preventing cloud-init
from replacing or locking existing accounts. It can also enable password SSH, so account retention
is not a substitute for the separate key-only policy check. Reapply this reviewed acceptance step
to every newly morphed deployment. Retain `machine-id` for this Relocate reference; keep rehearsals
isolated from productive traffic and identity-based integrations. Use the cloud server UUID as the
acceptance identity. A workload requiring simultaneous distinct OS identities needs a separate,
reviewed identity strategy; this helper does not regenerate machine identity on a running guest.

Use `workload-evidence.py compare --expected-json ... --expected-server-id ...` on the target
for independent SQL/API, seed, mount UUID and cloud-identity acceptance. The retained rehearsal
passed with 1,004 records and the source digest; the check performs no writes. The agent's
`provisioned` flag and running service are separate from a completed remote-command test.
After the operator's one-time destination-project Agent Service activation, the retained target
passed a read-only `RunShellScript` agent test with exit code 0, confirming root execution,
the expected kernel and KVM. Monitoring ingestion was not tested.

## Controlled delta validation

Only the explicitly confirmed VMware source may receive synthetic writes or a final freeze.
`workload-evidence.py write` and `freeze` require `--expected-source-uuid`, the confirmed VMware
BIOS UUID. The guard handles Linux SMBIOS byte ordering and rejects KVM even with a matching
hostname/UUID. Keep the writer timer stopped and record an independently verified baseline first.

```sh
umask 077
.local/venv/bin/python scripts/lab-remote.py source \
  --carrier-host "$CARRIER_HOST" --script scripts/workload-evidence.py \
  --script-args compare > .local/pilot/pre-delta-source.json
```

Run one reviewed batch only; do not overwrite its evidence or retry after an uncertain result:

```bash
[[ ! -e .local/pilot/delta-write.json ]] && \
.local/venv/bin/python scripts/lab-remote.py source \
  --carrier-host "$CARRIER_HOST" --script scripts/workload-evidence.py \
  --script-args write --writes 10 --expected-source-uuid "$SOURCE_BIOS_UUID" \
  --expected-json "$(cat .local/pilot/pre-delta-source.json)" \
  > .local/pilot/delta-write.json
```

The helper checks the supplied baseline before its first POST, then checks SQL/API again. Persist
its `after` object as the private new boundary and require that exact boundary at the new target.

```sh
.local/venv/bin/python -c \
  'import json, sys; print(json.dumps(json.load(sys.stdin)["after"], indent=2))' \
  < .local/pilot/delta-write.json > .local/pilot/delta-boundary.json
.local/venv/bin/python scripts/coriolis-pilot.py sync-transfer \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
.local/venv/bin/python scripts/coriolis-pilot.py status \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
```

Do not start the next deployment until this delta execution and both cleanup tasks are COMPLETED:

```sh
.local/venv/bin/python scripts/coriolis-pilot.py deploy-delta-rehearsal \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID"
```

This action permits one cloned-disk rehearsal per journaled delta execution, validates previous
deployment ownership/completion and refuses uncertain or duplicate requests. It retains the old
rehearsal; it does not replace that server or switch traffic. New targets start private. Establish
their own trusted host keys or use their exact server's authenticated Agent command path, then
repeat hardening and SQL/API/mount/cloud-ID acceptance against `delta-boundary.json`.

Private targets can use the reviewed Agent acceptance helper without another Public IP:

```sh
.local/venv/bin/python scripts/coriolis-pilot.py deployment-evidence \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID" --deployment-id "$DEPLOYMENT_ID" \
  --output .local/pilot/delta-rehearsal-deployment.json
.local/venv/bin/python scripts/agent-acceptance.py start \
  --target-server "$TARGET_SERVER_ID" --ssh-source "$OPERATOR_IPV4" \
  --deployment-evidence .local/pilot/delta-rehearsal-deployment.json
.local/venv/bin/python scripts/agent-acceptance.py status
```

It requires the completed own Deployment's exact live NICs/volumes and a provisioned Agent.
The read-only export rejects foreign/incomplete deployments, saves full evidence privately and
prints only an ID/path summary. Resolve `TARGET_SERVER_ID` by the evidence NIC and volume IDs,
not the duplicated source-derived server name.
Keep its journal and original evidence. Do not repeat `start` after uncertain submission.
`resume` permits only the same pending unsubmitted payload after an empty live command list;
a terminally failed command requires a new record and explicit `--prior-record` linkage. Live
command 399684 passed at the retained private delta target after the socket-runtime/transport fixes.
Agent output is bounded; the helper compresses the required JSON evidence, not the acceptance gates.

On a socket-activated guest, target hardening may first load unit definitions and start the existing
SSH service to create `/run/sshd`. It also adds a scoped `SuccessExitStatus=143` app-unit drop-in,
so Java's normal SIGTERM exit is not misclassified as a service failure. It does not restart the app
or PostgreSQL. Ordinary SSH trust/account/data checks remain mandatory when opening SSH later.

## Final cutover, validation and recovery

Final cutover requires a separately approved write freeze and normal source shutdown. Stop the
writer and application, stop PostgreSQL cleanly, record the final data evidence, then run a final
Transfer execution and a separately controlled Deployment. Never run both copies as writers.
The retained source must not be deleted, reset or restarted automatically after target acceptance.

The retained pilot has passed initial and controlled-delta deployment/data acceptance, including
private Agent verification. Source freeze, normal shutdown and final copy/cleanup also passed;
the final private deployment and exact target OS/data/Agent acceptance passed as well. The source
remains off and retained. This is a completed scoped lab migration, not production support,
estate-scale qualification or authorization to switch traffic/delete sources.

After accepted delta evidence, freeze only the explicitly identified source and preserve the result:

```sh
.local/venv/bin/python scripts/lab-remote.py source --carrier-host "$CARRIER_HOST" \
  --script scripts/workload-evidence.py --script-args freeze \
  --expected-source-uuid "$SOURCE_BIOS_UUID" \
  --expected-json "$(cat .local/pilot/delta-boundary.json)" \
  > .local/pilot/final-source-freeze.json
.local/venv/bin/python scripts/shutdown-source.py --host "$ESXI_HOST" \
  --expected-source-uuid "$SOURCE_BIOS_UUID" \
  --acceptance .local/pilot/delta-acceptance-agent-retry.json
```

Review that read-only shutdown plan, then add `--apply` only inside the approved lab window.
The helper calls only normal `ShutdownGuest`, journals before submission and refuses duplicates.
Verify actual `poweredOff` through the read-only ESXi inspector; a request is not completed shutdown.
Do not overwrite freeze evidence or rerun the freeze on an already frozen source.

```sh
.local/venv/bin/python scripts/coriolis-pilot.py final-transfer \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID" --esxi-host "$ESXI_HOST" \
  --confirm-source-uuid "$SOURCE_BIOS_UUID"
```

Only after this exact final execution and both cleanup tasks complete:

```sh
.local/venv/bin/python scripts/coriolis-pilot.py deploy-final \
  --url "$CORIOLIS_URL" --credentials-file "$CORIOLIS_CREDENTIALS" \
  --source-endpoint "$SOURCE_ENDPOINT_ID" --esxi-host "$ESXI_HOST" \
  --confirm-source-uuid "$SOURCE_BIOS_UUID"
```

Both actions independently verify the exact source remains poweredOff. Keep the frozen boundary
unchanged and pass explicit `--acceptance-evidence`/`--freeze-evidence` if using other private paths.
Repeat exact final-server hardening/Agent/data acceptance using the frozen boundary, not an earlier
rehearsal result. No action switches DNS, publishes a worker or deletes/restarts the retained source.

Rollback before target writes means stopping/isolation of the destination and resuming the retained
source. After target writes, rollback requires explicit data reconciliation; powering on the old
source is not a lossless reverse migration. Source retention and source deletion need separate approval.

After accepted cutover, hand measured compute/storage behavior to Optimize. Observability,
backup policies, load balancers, DNS switches and high availability are separate target controls;
their successful configuration is not implied by this small disk-copy pilot.

## Evidence and cleanup

- `.local/pilot/network.json`: exact own network, group, rule and carrier NIC identities.
- `.local/pilot/coriolis.json`: own endpoint, transfer, executions/deployments and pre-existing job baseline.
- `.local/esxi/source-*.json`: source identity, API snapshot/CBT acceptance and direct export grants.
- `PLAN.md`: sanitized tested state, open gates and explicit retention decisions.

Keep resources while a recorded job is active. Cleanup must use exact owned IDs, detach the added
carrier NIC before deleting it and retain both the existing appliance and source carrier.
Never delete an entire project, flush global nftables rules or bulk-delete Coriolis jobs.