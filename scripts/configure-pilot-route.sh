#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]]
role=${1:?Usage: configure-pilot-route.sh carrier MAC | appliance}
python3 - "$role" "${2:-}" <<'PY'
import configparser
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

role, mac = sys.argv[1:]
prefix = '10.77.242.0/24'
address = '10.77.242.50'
if role not in ('carrier', 'appliance'):
    raise SystemExit('Expected carrier or appliance role')
routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
for route in routes:
    if route.get('dst', 'default') == 'default':
        continue
    if ipaddress.ip_network(prefix).overlaps(ipaddress.ip_network(route['dst'], strict=False)):
        if role == 'appliance' and route.get('dev') != 'scf-esxi-wg':
            raise SystemExit('Pilot route overlaps another appliance route')

if role == 'carrier':
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
        raise SystemExit('Expected the recorded carrier NIC MAC')
    matches = [entry for entry in json.loads(subprocess.check_output(['ip', '-j', 'link']))
               if entry.get('address') == mac]
    if len(matches) != 1:
        raise SystemExit('Carrier NIC must resolve uniquely')
    interface = matches[0]['ifname']
    if any(route.get('dst') == prefix and route.get('dev') != interface for route in routes):
        raise SystemExit('Pilot route belongs to another carrier interface')
    network_file = Path('/etc/systemd/network/05-scf-relocate-pilot.network')
    network = f'[Match]\nMACAddress={mac}\n\n[Network]\nAddress={address}/24\nDHCP=no\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n'
    if network_file.exists() and network_file.read_text() != network:
        raise SystemExit('Refusing to overwrite a different NIC configuration')
    network_file.write_text(network)
    subprocess.run(['networkctl', 'reload'], check=True)
    subprocess.run(['networkctl', 'reconfigure', interface], check=True)
    subprocess.run(['ip', 'link', 'set', interface, 'up'], check=True)
    subprocess.run(['ip', 'address', 'replace', address + '/24', 'dev', interface], check=True)
    firewall = Path('/etc/scf-workload-egress.nft')
    original = firewall.read_text()
    backup = firewall.with_suffix('.nft.pre-pilot')
    if not backup.exists():
        backup.write_text(original)
        backup.chmod(0o600)
    anchor = '        iifname "scf-esxi-wg" drop\n        oifname "scf-esxi-wg" drop'
    additions = (
        f'        iifname "scf-esxi-wg" oifname "{interface}" ip saddr 10.77.241.2 ip daddr {prefix} tcp dport {{ 22, 5566 }} ct state new,established accept\n'
        f'        iifname "{interface}" oifname "scf-esxi-wg" ip saddr {prefix} ip daddr 10.77.241.2 ct state established,related accept\n')
    nat_anchor = '        type nat hook postrouting priority srcnat; policy accept;\n'
    nat_rule = f'        ip saddr 10.77.241.2 ip daddr {prefix} oifname "{interface}" snat to {address}\n'
    updated = original
    if additions not in original:
        if original.count(anchor) != 1:
            raise SystemExit('Expected scoped firewall boundary not found')
        updated = updated.replace(anchor, additions + anchor)
    if nat_rule not in original:
        if updated.count(nat_anchor) != 1:
            raise SystemExit('Expected scoped NAT boundary not found')
        updated = updated.replace(nat_anchor, nat_anchor + nat_rule)
    temporary = firewall.with_suffix('.pilot-check')
    temporary.write_text(updated)
    subprocess.run(['nft', '-c', '-f', str(temporary)], check=True)
    temporary.replace(firewall)
    subprocess.run(['systemctl', 'restart', 'scf-workload-egress'], check=True)
    print(json.dumps({'role': role, 'interface': interface, 'address': address, 'prefix': prefix,
                      'default_route': subprocess.check_output(['ip', '-4', 'route', 'show', 'default'], text=True).strip()}))
else:
    path = Path('/etc/wireguard/scf-esxi-wg.conf')
    original = path.read_text()
    if not original.startswith('# Managed by SCF Relocate lab connectivity'):
        raise SystemExit('Refusing to change an unmanaged WireGuard configuration')
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read_string(original)
    allowed = [item.strip() for item in config['Peer']['AllowedIPs'].split(',')]
    if '10.0.2.15/32' not in allowed:
        raise SystemExit('Expected existing ESXi route is missing')
    if prefix not in allowed:
        backup = path.with_suffix('.conf.pre-pilot')
        if not backup.exists():
            backup.write_text(original)
            backup.chmod(0o600)
        allowed.append(prefix)
        config['Peer']['AllowedIPs'] = ', '.join(allowed)
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(original.splitlines()[0] + '\n')
            config.write(stream)
    subprocess.run(['wg', 'set', 'scf-esxi-wg', 'peer', config['Peer']['PublicKey'],
                    'allowed-ips', ','.join(allowed)], check=True)
    subprocess.run(['ip', 'route', 'replace', prefix, 'dev', 'scf-esxi-wg'], check=True)
    print(json.dumps({'role': role, 'prefix': prefix, 'allowed_ports': [22, 5566],
                      'default_route': subprocess.check_output(['ip', '-4', 'route', 'show', 'default'], text=True).strip()}))
PY