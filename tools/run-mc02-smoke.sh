#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
elf="$root_dir/build/smoke/dm_mc02_smoke.elf"

if [[ ${1:-} == --qemu ]]; then
    [[ $# -ge 2 ]] || { printf '%s\n' 'usage: --qemu <path>' >&2; exit 2; }
    qemu_bin=$2
    shift 2
fi
if (($# != 0)); then
    printf 'usage: %s [--qemu <path>]\n' "$0" >&2
    exit 2
fi
[[ -x "$qemu_bin" ]] || { printf 'blocked: custom QEMU binary not found: %s\n' "$qemu_bin" >&2; exit 1; }
if [[ ! -x "$elf" ]]; then
    "$script_dir/build-smoke.sh" >/dev/null
fi
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required for QMP smoke' >&2; exit 1; }

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02,board-profile=DM-MC02 -kernel "$elf" -nodefaults \
    -display none -monitor none -serial none \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys

path = sys.argv[1]
sock = QmpSession(path, timeout=2.0)

command = sock.command_raw

profile = command("qom-get", {"path": "/machine", "property": "board-profile"})
if profile.get("return") != "DM-MC02":
    raise SystemExit("board profile selection mismatch: " + repr(profile))
if "error" in command("system_reset"):
    raise SystemExit("QMP system_reset failed")
reset_response = command("human-monitor-command",
                         {"command-line": "xp /1wx 0x58020814"})
if "error" in reset_response:
    raise SystemExit("reset GPIO query failed: " + str(reset_response))
reset_words = [int(value, 16) for value in re.findall(
    r"0x([0-9a-fA-F]{8})", reset_response.get("return", ""))]
if reset_words[:1] != [0x00000009]:
    raise SystemExit("system_reset did not restore BMI088 CS state: " +
                     repr(reset_words))
response = command("human-monitor-command", {"command-line": "xp /4wx 0x20000000"})
if "error" in response:
    raise SystemExit("guest memory query failed: " + str(response))
text = response.get("return", "")
words = [int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{8})", text)]
expected = [0x444D4331, 0x20020000]
if words[:2] != expected:
    raise SystemExit(f"vector/startup markers mismatch: {words!r}; output={text!r}")
print("RESULT: dm-mc02 machine smoke passed (including QMP system_reset)")
print("  marker/vector words:", " ".join(f"0x{x:08x}" for x in words[:4]))
PY
