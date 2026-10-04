#!/usr/bin/env python3
"""Run lab checks through the carrier using pinned SSH host keys only."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import shlex
import sys

import paramiko


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['carrier', 'source', 'esxi', 'destination'])
    parser.add_argument('--carrier-host', required=True)
    parser.add_argument('--destination-host')
    parser.add_argument('--destination-known-hosts')
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument('--command')
    commands.add_argument('--script')
    parser.add_argument('--script-args', nargs=argparse.REMAINDER, default=[])
    parser.add_argument('--local-dir', default='.local')
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    local = Path(args.local_dir)
    with ExitStack() as stack:
        carrier = paramiko.SSHClient()
        stack.callback(carrier.close)
        carrier.load_host_keys(str(local / 'esxi/known_hosts'))
        carrier.connect(args.carrier_host, username='probe', key_filename=str(local / 'esxi/ssh-key'),
                        look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=15, auth_timeout=30)
        remote = carrier
        if args.target != 'carrier':
            host = {'source': '10.0.2.20', 'esxi': '10.0.2.15',
                    'destination': args.destination_host}[args.target]
            if not host:
                parser.error('Destination access requires --destination-host')
            known_hosts = args.destination_known_hosts if args.target == 'destination' else str(
                local / ('esxi/esxi-known-hosts' if args.target == 'esxi' else 'workload/known-hosts'))
            if not known_hosts:
                parser.error('Destination access requires an explicitly verified host-key file')
            channel = carrier.get_transport().open_channel('direct-tcpip', (host, 22), ('127.0.0.1', 0), timeout=15)
            stack.callback(channel.close)
            remote = paramiko.SSHClient()
            stack.callback(remote.close)
            remote.load_host_keys(known_hosts)
            remote.connect(host, username='root' if args.target == 'esxi' else 'ubuntu', sock=channel,
                           password=(local / 'esxi/root-password').read_text() if args.target == 'esxi' else None,
                           key_filename=None if args.target == 'esxi' else str(local / 'workload/ssh-key'),
                           look_for_keys=False, allow_agent=False, timeout=15, banner_timeout=15, auth_timeout=30)
        command = args.command
        if args.script:
            interpreter = 'python3 - ' if Path(args.script).suffix == '.py' else 'bash -s -- '
            command = ('' if args.target == 'esxi' else 'sudo -n ') + interpreter + shlex.join(args.script_args)
        stdin, stdout, stderr = remote.exec_command(command, timeout=300)
        if args.script:
            stdin.write(Path(args.script).read_text())
        stdin.channel.shutdown_write()
        sys.stdout.write(stdout.read().decode())
        sys.stderr.write(stderr.read().decode())
        raise SystemExit(stdout.channel.recv_exit_status())


if __name__ == '__main__':
    main()