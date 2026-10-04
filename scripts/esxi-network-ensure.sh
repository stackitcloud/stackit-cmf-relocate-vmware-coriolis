#!/usr/bin/env bash
set -euo pipefail
if ! ip link show scf-esxi-br >/dev/null 2>&1; then
    exec /usr/local/sbin/esxi-network.sh
fi
python3 - <<'CHECK'
import json,subprocess
links=json.loads(subprocess.check_output(['ip','-j','-d','link','show']))
byname={x['ifname']:x for x in links}
br=byname['scf-esxi-br'];tap=byname.get('scf-esxi-tap',{})
assert br['linkinfo']['info_kind']=='bridge'
assert tap.get('master')=='scf-esxi-br'
assert tap.get('linkinfo',{}).get('info_kind')=='tun'
assert {x['ifname'] for x in links if x.get('master')=='scf-esxi-br'}=={'scf-esxi-tap'}
addr=json.loads(subprocess.check_output(['ip','-j','-4','addr','show','dev','scf-esxi-br']))
assert any(a['local']=='10.0.2.2' and a['prefixlen']==24 for a in addr[0]['addr_info'])
CHECK
ip link set scf-esxi-br up
ip link set scf-esxi-tap up
