#!/usr/bin/env bash
# Carrier-local management network; no public listener, uplink bridge or routing.
set -euo pipefail
[[ $EUID -eq 0 ]]
if ip link show scf-esxi-br >/dev/null 2>&1 || ip link show scf-esxi-tap >/dev/null 2>&1; then
  echo 'Lab interfaces already exist; inspect them instead of overwriting.' >&2
  exit 1
fi
python3 - <<'PY'
import ipaddress,json,subprocess
lab=ipaddress.ip_network('10.0.2.0/24')
for route in json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','all'])):
    dst=route.get('dst','default')
    if dst != 'default' and lab.overlaps(ipaddress.ip_network(dst,strict=False)):
        raise SystemExit('Existing route overlaps lab subnet; refusing changes')
PY
ip link add scf-esxi-br type bridge
ip addr add 10.0.2.2/24 dev scf-esxi-br
ip link set scf-esxi-br up
ip tuntap add dev scf-esxi-tap mode tap
ip link set scf-esxi-tap master scf-esxi-br
ip link set scf-esxi-tap up
ip -br addr show scf-esxi-br
