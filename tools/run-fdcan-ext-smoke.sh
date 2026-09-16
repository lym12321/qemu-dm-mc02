#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null
command -v python3 >/dev/null
[[ -x "$qemu_bin" ]]
run_dir=$(mktemp -d "$root_dir/output.dm-mc02-fdcan-ext.XXXXXX")
qmp_socket="$run_dir/qmp.sock"; can_socket="$run_dir/can.sock"; guest_elf="$run_dir/ext.elf"
qemu_pid=''
cleanup() { [[ -n "$qemu_pid" ]] && kill "$qemu_pid" 2>/dev/null || true; wait "$qemu_pid" 2>/dev/null || true; rm -rf -- "$run_dir"; }
trap cleanup EXIT
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin -fno-stack-protector \
  -nostdlib -nostartfiles -Wl,--gc-sections -Wl,--build-id=none \
  -Wl,-T,"$root_dir/smoke/dm_mc02_fdcan_ext_smoke.ld" -o "$guest_elf" \
  "$root_dir/smoke/dm_mc02_fdcan_ext_smoke.c"
"$qemu_bin" -machine dm-mc02 -kernel "$guest_elf" -nodefaults -display none -monitor none \
  -serial none -serial none -serial none -serial none -serial none -serial none -serial none \
  -chardev "socket,id=can,path=$can_socket,server=on,wait=off" -serial chardev:can \
  -qmp "unix:$qmp_socket,server=on,wait=off" >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 300); do [[ -S "$can_socket" && -S "$qmp_socket" ]] && break; sleep 0.01; done
[[ -S "$can_socket" && -S "$qmp_socket" ]]
python3 - "$can_socket" "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re, socket, struct, sys, time
can_path, qmp_path = sys.argv[1:]
can = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); can.settimeout(2); can.connect(can_path)
qmp = QmpSession(qmp_path, timeout=2)
def cmd(name, args=None):
    return qmp.command(name, args)
def words(addr, count):
    s=cmd("human-monitor-command", {"command-line":f"xp /{count}wx {addr}"}) or ""
    return [int(x,16) for x in re.findall(r"0x([0-9a-fA-F]{8})",s)]
cmd("cont")
def frame(can_id, payload):
    b=bytearray(84); struct.pack_into("<IIB",b,0,can_id,1,len(payload)); b[20:20+len(payload)]=payload; return b
can.sendall(frame(0x777777,b"N")); time.sleep(.03)
if words("0x4000a0b4",1)[0] & 0x7f: raise RuntimeError("nonmatching extended frame entered FIFO1")
can.sendall(frame(0x123456,b"FIFO1"))
deadline=time.monotonic()+2
while time.monotonic()<deadline:
    w=words("0x20000000",8)
    if len(w)>=8 and w[0]==0x45584631:
        if w[2] != (0x123456 | (1 << 30)) or w[3] != 0x4f464946 or w[4] != 0x31:
            raise RuntimeError(f"bad FIFO1 readback: {w}")
        if not (w[5] & (1<<4)) or not (w[5] & (1<<8)) or (w[6] & 0x7f):
            raise RuntimeError(f"bad IRQ/ack: {w}")
        if w[7] != 0x80c0:
            raise RuntimeError(f"bad HPMS: {w}")
        print("FDCAN extended filter/FIFO1 smoke passed")
        cmd("quit"); break
    time.sleep(.01)
else:
    raise RuntimeError("extended frame was not routed to FIFO1: " + repr({
        "rxf0s": words("0x4000a0a4", 1),
        "rxf1s": words("0x4000a0b4", 1),
        "gfc": words("0x4000a080", 1),
        "xidfc": words("0x4000a088", 1),
        "filter": words("0x4000ae00", 2),
        "result": words("0x20000000", 7),
        "status": cmd("query-status"),
    }))
can.close(); qmp.close()
PY
