#!/usr/bin/env bash
set -euo pipefail

iso=${1:?Usage: esxi-boot.sh /absolute/path/to/installer.iso [create|reuse|installed]}
mode=${2:-create}
[[ "$mode" = create || "$mode" = reuse || "$mode" = installed ]]
workdir=/var/lib/scf-esxi
expected=9782c96ffd01cc56da17ec31573da69f4cba2f9402e67c8b55d05d9472c7376a

[[ $EUID -eq 0 ]]
[[ "$iso" = /* && -f "$iso" ]]
printf '%s  %s\n' "$expected" "$iso" | sha256sum --check
grep -qw vmx /proc/cpuinfo
grep -qw ept /proc/cpuinfo
modprobe kvm_intel
[[ -c /dev/kvm ]]
[[ $(< /sys/module/kvm_intel/parameters/nested) = Y ]]
[[ $(< /sys/module/kvm_intel/parameters/ept) = Y ]]
printf 'SCF_ESXI_VMX_EPT_READY\n'

install -d -m 0700 "$workdir"
[[ ! -e "$workdir/qmp.sock" ]]
if [[ "$mode" = create ]]; then
  [[ ! -e "$workdir/system.qcow2" && ! -e "$workdir/datastore.qcow2" ]]
  qemu-img create -f qcow2 "$workdir/system.qcow2" 32G
  qemu-img create -f qcow2 "$workdir/datastore.qcow2" 32G
else
  [[ -f "$workdir/system.qcow2" && -f "$workdir/datastore.qcow2" ]]
  qemu-img check "$workdir/system.qcow2"
  qemu-img check "$workdir/datastore.qcow2"
fi

if [[ "$mode" = installed ]]; then
  boot_args=(-boot order=c,menu=off)
else
  boot_args=(-cdrom "$iso" -boot order=d,menu=off)
fi

ip link show scf-esxi-tap >/dev/null

systemd-run --unit=scf-esxi-boot --property=RuntimeMaxSec=3600 \
  --property="WorkingDirectory=$workdir" \
  qemu-system-x86_64 \
  -name scf-esxi-compatibility -machine q35 -accel kvm -cpu host,vmx=on \
  -smp 4,sockets=1,cores=4,threads=1 -m 10240 \
  -no-user-config -nodefaults -display none -vga std \
  -qmp "unix:$workdir/qmp.sock,server=on,wait=off" \
  -serial "file:$workdir/serial.log" \
  -drive "file=$workdir/system.qcow2,if=none,id=system,format=qcow2" \
  -device ide-hd,bus=ide.0,drive=system,serial=SCFESXISYSTEM \
  -drive "file=$workdir/datastore.qcow2,if=none,id=datastore,format=qcow2" \
  -device ide-hd,bus=ide.1,drive=datastore,serial=SCFESXIDATASTORE \
  "${boot_args[@]}" \
  -netdev tap,id=management,ifname=scf-esxi-tap,script=no,downscript=no \
  -device vmxnet3,netdev=management,mac=52:54:00:12:34:56