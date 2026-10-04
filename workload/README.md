# Relocate acceptance workload

One Ubuntu 24.04 guest with Java 21, Spring Boot 4.1.1 and PostgreSQL 16.
The PostgreSQL cluster resides on the separate data disk at
`/srv/relocate-data/postgresql`. System/application files reside on the boot disk.
The database and HTTP API listen locally. Access through authenticated guest SSH;
this synthetic workload has no public listener or application authentication.

- `GET http://127.0.0.1:8080/actuator/health`: application and database health.
- `GET http://127.0.0.1:8080/api/evidence`: total count, seed count, write count,
  and digest of ordered record IDs and payloads.
- `POST http://127.0.0.1:8080/api/writes`: one synthetic write with a unique ID.

The initial 1,000 seed records are deterministic. Startup inserts only missing
seeds and preserves prior writes. The `relocate-writer.timer` generates one
write every ten seconds when enabled. Its enabled/disabled state must be recorded
for every rehearsal and cutover; do not run source and target writers together.

Freeze before taking final evidence:

```sh
sudo systemctl stop relocate-writer.timer
sudo systemctl stop relocate-writer.service
curl -fsS http://127.0.0.1:8080/api/evidence
```

This stops the bundled writer only. Other API callers must also be stopped.
Compare the frozen source digest and counts to the isolated target after
migration. Verify data-disk mount/UUID, PostgreSQL data_directory, boot, guest tools
and health separately. A digest match is not proof of application-consistent
snapshots or a supported Coriolis migration.

Guest provisioning is in `scripts/setup-workload.sh`; it refuses an unexpected
hostname/disk size/partition layout and only formats the new blank 8-GiB disk.
Database credentials are generated on the guest in root-only
`/etc/relocate-demo.env`; SSH keys and seed media remain under ignored `.local/`.
