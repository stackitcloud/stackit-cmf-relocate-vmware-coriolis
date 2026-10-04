#!/usr/bin/env bash
# Root-only; peer argument is PUBLIC. Private keys are generated and kept on each host.
set -euo pipefail
[[ $EUID -eq 0 ]]
role=${1:?Usage: setup-esxi-wireguard.sh carrier|appliance PEER_PUBLIC_KEY}
peer=${2:?Missing peer public key}
[[ "$role" = carrier || "$role" = appliance ]]
[[ -f /etc/wireguard/scf-esxi.key ]]
python3 - "$role" "$peer" <<'PY'
import base64,ipaddress,json,os,subprocess,sys
from pathlib import Path
role,peer=sys.argv[1:]
assert len(base64.b64decode(peer,validate=True))==32
pool=ipaddress.ip_network('10.77.241.0/30')
for route in json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','all'])):
    dst=route.get('dst','default')
    if dst!='default' and pool.overlaps(ipaddress.ip_network(dst,strict=False)):
        assert route.get('dev')=='scf-esxi-wg', 'Existing route overlaps WireGuard addresses'
path=Path('/etc/wireguard/scf-esxi-wg.conf')
marker='# Managed by SCF Relocate lab connectivity\n'
if path.exists():
    assert path.read_text().startswith(marker), 'Refusing to overwrite unmanaged configuration'
private=Path('/etc/wireguard/scf-esxi.key').read_text().strip()
assert len(base64.b64decode(private,validate=True))==32
address='10.77.241.1/32' if role=='carrier' else '10.77.241.2/32'
config=marker+f'[Interface]\nAddress = {address}\nPrivateKey = {private}\nMTU = 1280\n'
if role=='carrier':config+='ListenPort = 51820\n'
config+=f'\n[Peer]\nPublicKey = {peer}\n'
if role=='carrier':config+='AllowedIPs = 10.77.241.2/32\n'
else:config+='AllowedIPs = 10.0.2.15/32\nEndpoint = 188.34.107.145:51820\nPersistentKeepalive = 25\n'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:f.write(config)
os.chmod(path,0o600)
PY
if [[ "$role" = carrier ]]; then
    install -d /etc/systemd/system/wg-quick@scf-esxi-wg.service.d
    cat > /etc/systemd/system/wg-quick@scf-esxi-wg.service.d/firewall.conf <<'UNIT'
[Unit]
Requires=scf-workload-egress.service
After=scf-workload-egress.service
UNIT
fi
systemctl daemon-reload
systemctl enable --now wg-quick@scf-esxi-wg
ip -br address show scf-esxi-wg
wg show scf-esxi-wg public-key
wg show scf-esxi-wg latest-handshakes
