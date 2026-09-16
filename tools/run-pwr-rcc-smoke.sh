#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
elf="$root_dir/build/smoke/dm_mc02_pwr_rcc_smoke.elf"

command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
command -v timeout >/dev/null 2>&1 || { printf '%s\n' 'blocked: timeout is required' >&2; exit 1; }
[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
[[ -x "$elf" ]] || "$script_dir/build-pwr-rcc-smoke.sh" >/dev/null

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/dm-mc02-pwr-rcc.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -f -- "$qmp_socket" "$run_dir/qemu.stderr"
    rmdir -- "$run_dir"
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02 -kernel "$elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 100); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

timeout 10s python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command

text = command("human-monitor-command", {"command-line": "xp /20wx 0x20000000"})
words = [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text or "")]
if len(words) < 20:
    raise RuntimeError("marker query returned too few words: %r" % text)
if words[0] == 0x46415531:
    raise RuntimeError("guest entered HardFault, IPSR=0x%08x" % words[11])
if words[0] != 0x50575231:
    raise RuntimeError("PWR/RCC status check failed: marker=0x%08x words=%r" %
                       (words[0], words))
if not (words[3] & (1 << 13)):
    raise RuntimeError("PWR CSR1 ACTVOSRDY is clear: 0x%08x" % words[3])
if not (words[5] & (1 << 2)):
    raise RuntimeError("RCC CR HSIRDY is clear: 0x%08x" % words[5])
if words[12] != 0x00000002 or (words[12] & (7 << 3)) != 0:
    raise RuntimeError("unready HSE request changed SWS: 0x%08x" % words[12])
if words[13] & (1 << 17):
    raise RuntimeError("unready HSE reports HSERDY: 0x%08x" % words[13])
if words[14] != 0x00000012 or not (words[15] & (1 << 17)):
    raise RuntimeError("ready HSE did not become effective: CFGR=0x%08x CR=0x%08x" %
                       (words[14], words[15]))
if words[16] != 0x00000003 or (words[16] & (7 << 3)) != 0:
    raise RuntimeError("invalid PLL request changed SWS: 0x%08x" % words[16])
if words[17] & (1 << 25):
    raise RuntimeError("invalid PLL reports PLL1RDY: 0x%08x" % words[17])
if words[18] != 0x0000001b or not (words[19] & (1 << 25)):
    raise RuntimeError("valid PLL request did not become effective: CFGR=0x%08x CR=0x%08x" %
                       (words[18], words[19]))
print("RESULT: dm-mc02 PWR/RCC bare-metal smoke passed")
print("  marker: 0x%08x" % words[0])
print("  PWR CR3/D3CR/CSR1/CPUCR: %s" % " ".join("0x%08x" % x for x in words[1:5]))
print("  RCC CR/CFGR/D1CFGR/D2CFGR/D3CFGR/AHB4ENR: %s" %
      " ".join("0x%08x" % x for x in words[5:11]))
command("quit")
PY
