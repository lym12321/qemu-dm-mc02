#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU binary not found: %s\n' "$qemu_bin" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required for QMP smoke' >&2
    exit 1
}

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket qmp \
    --python-arg "{qmp}" -- \
    "$qemu_bin" -machine dm-mc02,vin-mv=12000 -nodefaults \
    -display none -monitor none -serial none -S \
    -qmp "unix:{qmp},server=on,wait=off" <<'PY'
from dm_mc02_qmp import QmpSession
import sys

path = sys.argv[1]
sock = QmpSession(path, timeout=2.0)

command = sock.command_raw

response = command("qom-get", {"path": "/machine", "property": "vin-mv"})
if response.get("return") != "12000":
    raise SystemExit("machine VIN property mismatch: %r" % response)

response = command("qom-set", {"path": "/machine", "property": "vin-mv",
                                "value": "18000"})
if "error" in response:
    raise SystemExit("runtime VIN update failed: %r" % response)
response = command("qom-get", {"path": "/machine", "property": "vin-mv"})
if response.get("return") != "18000":
    raise SystemExit("runtime VIN readback mismatch: %r" % response)

response = command("system_reset")
if "error" in response:
    raise SystemExit("system_reset failed: %r" % response)
response = command("qom-get", {"path": "/machine", "property": "vin-mv"})
if response.get("return") != "18000":
    raise SystemExit("VIN was not preserved across reset: %r" % response)

print("RESULT: DM-MC02 VIN machine property/runtime/reset smoke passed")
PY
