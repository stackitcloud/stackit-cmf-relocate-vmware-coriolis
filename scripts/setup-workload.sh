#!/usr/bin/env bash
# Run only on the dedicated, freshly provisioned scf-relocate-app guest.
set -euo pipefail
[[ $EUID -eq 0 && $(hostname) = scf-relocate-app ]]
source_dir=${1:?Usage: setup-workload.sh /home/ubuntu/scf-workload}
[[ -f "$source_dir/workload/pom.xml" ]]
python3 - <<'PY'
import json,subprocess
info=json.loads(subprocess.check_output(['lsblk','-J','-b','-o','NAME,SIZE,TYPE,MOUNTPOINTS','/dev/sdb']))['blockdevices']
assert len(info)==1 and info[0]['type']=='disk' and info[0]['size']==8589934592
assert not info[0].get('children'), 'Unexpected data disk partitions'
PY
if ! blkid /dev/sdb >/dev/null 2>&1; then
    python3 - <<'PY'
import json,subprocess
assert not json.loads(subprocess.check_output(['wipefs','-n','-J','/dev/sdb']))['signatures']
assert not subprocess.check_output(['lsblk','-n','-o','MOUNTPOINTS','/dev/sdb']).strip()
PY
    mkfs.ext4 -L relocate-data /dev/sdb
fi
[[ $(blkid -s LABEL -o value /dev/sdb) = relocate-data ]]
[[ $(blkid -s TYPE -o value /dev/sdb) = ext4 ]]
mkdir -p /srv/relocate-data
uuid=$(blkid -s UUID -o value /dev/sdb)
if ! grep -q '^UUID=.* /srv/relocate-data ' /etc/fstab; then
    printf 'UUID=%s /srv/relocate-data ext4 defaults 0 2\n' "$uuid" >> /etc/fstab
fi
mountpoint -q /srv/relocate-data || mount /srv/relocate-data
[[ $(findmnt -n -o UUID /srv/relocate-data) = "$uuid" ]]
# Configure a single PostgreSQL cluster directly on the separate data disk.
install -d /etc/postgresql-common
if ! command -v pg_lsclusters >/dev/null; then
    printf 'create_main_cluster = false\n' > /etc/postgresql-common/createcluster.conf
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y postgresql-16 openjdk-21-jdk-headless maven open-vm-tools
if ! pg_lsclusters --no-header | grep -q '^16[[:space:]]\+relocate[[:space:]]'; then
    [[ ! -e /srv/relocate-data/postgresql ]]
    pg_createcluster 16 relocate --port=5432 --datadir=/srv/relocate-data/postgresql --start
fi
systemctl enable --now postgresql open-vm-tools
pg_ctlcluster 16 relocate status >/dev/null || pg_ctlcluster 16 relocate start
python3 - <<'PY'
import secrets,subprocess,os
from pathlib import Path
p=Path('/etc/relocate-demo.env')
if not p.exists():
    secret=secrets.token_hex(24)
    fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:f.write('RELOCATE_DB_PASSWORD='+secret+'\n')
secret=p.read_text().strip().split('=',1)[1]
assert len(secret)==48 and all(c in '0123456789abcdef' for c in secret)
# Supply credentials on stdin; no secret in process arguments or output.
sql=f"""SELECT 'CREATE ROLE relocate LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='relocate')\\gexec
ALTER ROLE relocate PASSWORD '{secret}';
SELECT 'CREATE DATABASE relocate OWNER relocate' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='relocate')\\gexec
"""
subprocess.run(['runuser','-u','postgres','--','psql','-v','ON_ERROR_STOP=1'],input=sql,text=True,stdout=subprocess.DEVNULL,check=True)
PY
id relocate >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin relocate
runuser -u ubuntu -- mvn -B -q -f "$source_dir/workload/pom.xml" package
install -d -m 755 /opt/relocate-demo
install -m 644 "$source_dir/workload/target/relocate-demo-0.0.1-SNAPSHOT.jar" /opt/relocate-demo/app.jar
install -m 644 "$source_dir/scripts/relocate-demo.service" "$source_dir/scripts/relocate-writer.service" "$source_dir/scripts/relocate-writer.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable relocate-demo
systemctl restart relocate-demo
# Writer is installed but intentionally not enabled until baseline checks pass.
