#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l

finish() {
  local status=${1:-$?}
  if (( status != 0 )); then
    printf 'SCF_NESTED_RESULT=FAIL exit=%s\n' "$status"
  fi
  printf 'SCF_NESTED_END\n'
}
trap finish EXIT

printf 'SCF_NESTED_BEGIN\n'
uname -m
lscpu
if ! grep -Eq '^flags[[:space:]]*:.* (vmx|svm)( |$)' /proc/cpuinfo; then
  printf 'SCF_NESTED_REASON=CPU_VIRTUALIZATION_NOT_EXPOSED\n'
  exit 10
fi

if grep -qw vmx /proc/cpuinfo; then
  modprobe kvm_intel
else
  modprobe kvm_amd
fi
test -c /dev/kvm
ls -l /dev/kvm
python3 - <<'PY'
import fcntl
import os

descriptor = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
try:
    version = fcntl.ioctl(descriptor, 0xAE00, 0)
    print(f'SCF_KVM_API_VERSION={version}', flush=True)
    if version != 12:
        raise RuntimeError('Unsupported KVM API version')
finally:
    os.close(descriptor)
PY

apt-get -qq -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update
apt-get -qq -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install -y --no-install-recommends qemu-system-x86
qemu-system-x86_64 --version
workdir=$(mktemp -d)
trap 'status=$?; rm -rf "$workdir"; finish "$status"' EXIT
python3 - "$workdir/boot.raw" <<'PY'
import pathlib
import sys

code = bytearray(b'\xba\xf8\x03')
for value in b'SCF_KVM_GUEST_OK\r\n':
    code.extend((0xB0, value, 0xEE))
code.extend(b'\xba\xf4\x00\xb0\x2a\xee\xf4\xeb\xfd')
image = code.ljust(510, b'\x00') + b'\x55\xaa'
pathlib.Path(sys.argv[1]).write_bytes(image.ljust(1440 * 1024, b'\x00'))
PY

set +e
timeout 45s qemu-system-x86_64 \
  -accel kvm -cpu host -smp 1 -m 128M \
  -nodefaults -no-user-config -display none -monitor none -serial stdio \
  -device isa-debug-exit,iobase=0xf4,iosize=0x04 \
  -drive "file=$workdir/boot.raw,format=raw,if=floppy,readonly=on" \
  -boot a -no-reboot > "$workdir/guest.log" 2>&1
guest_status=$?
set -e
cat "$workdir/guest.log"
if [[ "$guest_status" != 85 ]] || ! grep -q '^SCF_KVM_GUEST_OK' "$workdir/guest.log"; then
  printf 'SCF_NESTED_REASON=KVM_GUEST_EXECUTION_FAILED qemu_exit=%s\n' "$guest_status"
  exit 20
fi
printf 'SCF_NESTED_RESULT=PASS\n'