#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
elf="$root_dir/build/smoke/dm_mc02_bmi088_smoke.elf"
machine=${DM_MC02_MACHINE:-dm-mc02}
expected_temp_m=${BMI088_EXPECT_TEMP_M:-2}

[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
if [[ ! -x "$elf" || "$root_dir/smoke/dm_mc02_bmi088_smoke.c" -nt "$elf" ||
      "$root_dir/smoke/dm_mc02_bmi088_smoke.ld" -nt "$elf" ]]; then
    "$script_dir/build-bmi088-smoke.sh" >/dev/null
fi
python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine "$machine" -kernel "$elf" -nodefaults -display none \
    -monitor none -serial none -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import re
import sys

sock = QmpSession(sys.argv[1], timeout=2.0)

command = sock.command_raw

response = command("human-monitor-command", {"command-line": "xp /10wx 0x20000000"})
if "error" in response:
    raise SystemExit(str(response))
words = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})", response["return"])]
expected = [0x424D4932, 0x1E, 0x0F, 0x00, 0x40, 0x00, 0x00, 0x00,
            int(__import__("os").environ.get("BMI088_EXPECT_TEMP_M", "2"), 0), 0x00]
if words[:10] != expected:
    raise SystemExit(f"BMI088 SPI mismatch: {words[:10]!r}, expected {expected!r}")
print("RESULT: SPI2/BMI088 polled smoke passed")
print("  marker/id/raw/reset/temp:", " ".join(f"0x{x:08x}" for x in words[:10]))
PY
