#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null || { echo 'blocked: arm-none-eabi-gcc is required' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'blocked: python3 is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { echo "blocked: QEMU not found: $qemu_bin" >&2; exit 1; }
run_dir=$(mktemp -d "${TMPDIR:-/tmp}/dm-mc02-ws2812.XXXXXX")
trap 'rm -rf -- "$run_dir"' EXIT
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -mfloat-abi=soft -ffreestanding -fno-builtin \
  -fno-stack-protector -nostdlib -Wl,--build-id=none \
  -Wl,-T,"$root_dir/smoke/dm_mc02_ws2812_smoke.ld" \
  -o "$run_dir/ws2812.elf" "$root_dir/smoke/dm_mc02_ws2812_smoke.c"
python3 "$script_dir/dm_mc02_test_harness.py" --timeout 8 --expect-qemu-quit \
  --socket cosim --socket qmp \
  --python-arg "{cosim}" --python-arg "{qmp}" -- \
  "$qemu_bin" -machine dm-mc02 -kernel "$run_dir/ws2812.elf" \
  -nodefaults -display none -monitor none \
  -chardev "socket,id=cosim,path={cosim},server=on,wait=off" -serial chardev:cosim \
  -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import socket, struct, sys
c, q = sys.argv[1:]
def exact(s, n):
    b=b''
    while len(b)<n:
        x=s.recv(n-len(b))
        if not x: raise RuntimeError('chardev closed')
        b+=x
    return b
def recv(s):
    n=struct.unpack('<I',exact(s,4))[0]
    h=exact(s,28); magic,ver,kind,plen,seq,t=struct.unpack('<IHHIQQ',h)
    if (magic,ver,kind,plen)!=(0x32434d44,1,3,12) or n!=28+plen: raise RuntimeError('bad telemetry')
    return struct.unpack('<IIBBH',exact(s,plen))
s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(c)
first=recv(s)
if first==(0,0,0,0,0): first=recv(s)
if first!=(0x12a43c,0xa4,0,0,0): raise RuntimeError('first WS2812 frame: %r'% (first,))
print('WS2812 first: rgb=%06x brightness=%d'%first[:2])
updated=recv(s)
if updated!=(0xe70591,0xe7,0,0,0): raise RuntimeError('updated WS2812 frame: %r'% (updated,))
print('WS2812 update: rgb=%06x brightness=%d'%updated[:2])
z=QmpSession(q, timeout=2); z.command("quit"); z.close()
PY
echo 'RESULT: WS2812 telemetry smoke passed'
