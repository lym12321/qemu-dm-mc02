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

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-vin.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -f -- "$run_dir/qemu.stderr" "$qmp_socket"
    rmdir -- "$run_dir" 2>/dev/null || true
}
trap cleanup EXIT

"$qemu_bin" -machine dm-mc02,vin-mv=12000 -nodefaults \
    -display none -monitor none -serial none -S \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!
for _ in $(seq 1 100); do
    [[ -S "$qmp_socket" ]] && break
    sleep .01
done
if [[ ! -S "$qmp_socket" ]]; then
    printf '%s\n' 'VIN configuration smoke: QMP socket missing' >&2
    sed -n '1,40p' "$run_dir/qemu.stderr" >&2
    exit 1
fi

python3 - "$qmp_socket" <<'PY'
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
