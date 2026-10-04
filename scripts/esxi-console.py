#!/usr/bin/env python3
import argparse
import json
import re
import socket
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'screen', 'key', 'text'])
    parser.add_argument('value', nargs='?', help='absolute PNG path or key chord, e.g. alt+f12 / alt-f12')
    args = parser.parse_args()
    if args.action in ('screen', 'key') and not args.value:
        parser.error('screen requires an absolute PNG path; key requires a QEMU qcode')
    if args.action == 'screen' and not args.value.startswith('/'):
        parser.error('screen requires an absolute PNG path')
    keys = re.split(r'[+-]', args.value.lower()) if args.action == 'key' else []
    if keys and not all(keys):
        parser.error('key chord contains an empty key')
    # Text is read from stdin so passwords never become process arguments.
    text_keys = []
    if args.action == 'text':
        if args.value:
            parser.error('text reads from stdin, not a command-line argument')
        punctuation = {' ': 'spc', '\n': 'ret', '\t': 'tab', '-': 'minus',
                       '=': 'equal', '.': 'dot', '/': 'slash', ',': 'comma',
                       ';': 'semicolon', "'": 'apostrophe', '[': 'bracket_left',
                       ']': 'bracket_right', '\\': 'backslash', '`': 'grave_accent'}
        shifted = dict(zip('!@#$%^&*()_+<>?:"{}|~',
                           ['1','2','3','4','5','6','7','8','9','0','minus','equal',
                            'comma','dot','slash','semicolon','apostrophe',
                            'bracket_left','bracket_right','backslash','grave_accent']))
        for char in sys.stdin.read():
            if char.isascii() and char.isalnum():
                text_keys.append((['shift'] if char.isupper() else []) + [char.lower()])
            elif char in punctuation:
                text_keys.append([punctuation[char]])
            elif char in shifted:
                text_keys.append(['shift', shifted[char]])
            else:
                parser.error('text supports US keyboard ASCII characters only')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(20)
        connection.connect('/var/lib/scf-esxi/qmp.sock')
        stream = connection.makefile('rwb')
        greeting = json.loads(stream.readline())
        if 'QMP' not in greeting:
            raise RuntimeError('Missing QMP greeting')

        def execute(command, arguments=None):
            request = {'execute': command}
            if arguments:
                request['arguments'] = arguments
            stream.write((json.dumps(request) + '\n').encode())
            stream.flush()
            while True:
                line = stream.readline()
                if not line:
                    raise RuntimeError('QMP disconnected before replying')
                response = json.loads(line)
                if 'error' in response:
                    raise RuntimeError(json.dumps(response['error']))
                if 'return' in response:
                    return response['return']

        execute('qmp_capabilities')
        if args.action == 'status':
            print(json.dumps({'status': execute('query-status'), 'kvm': execute('query-kvm')}, indent=2))
        elif args.action == 'screen':
            execute('screendump', {'filename': args.value, 'format': 'png'})
        elif args.action == 'text':
            for chord in text_keys:
                execute('send-key', {'keys': [{'type': 'qcode', 'data': key} for key in chord], 'hold-time': 60})
                time.sleep(0.1)
        else:
            execute('send-key', {'keys': [{'type': 'qcode', 'data': keycode}
                                        for keycode in keys]})


if __name__ == '__main__':
    main()
