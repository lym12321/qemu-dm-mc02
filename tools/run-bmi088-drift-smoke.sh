#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-bmi088-drift.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
qemu_pid=''
cleanup() {
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
}
trap cleanup EXIT

"$qemu_bin" \
    -machine 'dm-mc02,imu-temperature-c=35,imu-temp-coeff-gyro-dps-per-c=0.1:-0.2:0.3,imu-temp-coeff-accel-g-per-c=0.01:0.02:-0.03,imu-bias-random-walk-gyro-dps-per-sqrt-s=0.4:0.5:0.6,imu-bias-random-walk-accel-g-per-sqrt-s=0.007:0.008:0.009' \
    -nodefaults -display none -monitor none -serial none -S \
    -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in $(seq 1 300); do
    [[ -S "$qmp_socket" ]] && break
    sleep 0.01
done
[[ -S "$qmp_socket" ]] || { sed -n '1,80p' "$run_dir/qemu.stderr" >&2; exit 1; }

python3 - "$qmp_socket" <<'PY'
from dm_mc02_qmp import QmpSession
import sys

qmp = QmpSession(sys.argv[1], timeout=2.0)

command = qmp.command

def get(property_name):
    return command("qom-get", {"path": "/machine", "property": property_name})

expected = {
    "imu-temperature-c": "35",
    "imu-temp-coeff-gyro-dps-per-c": "0.1,-0.2,0.3",
    "imu-temp-coeff-accel-g-per-c": "0.01,0.02,-0.03",
    "imu-bias-random-walk-gyro-dps-per-sqrt-s": "0.4,0.5,0.6",
    "imu-bias-random-walk-accel-g-per-sqrt-s": "0.007,0.008,0.009",
}
for property_name, wanted in expected.items():
    actual = get(property_name)
    if actual != wanted:
        raise RuntimeError(f"{property_name}: expected {wanted!r}, got {actual!r}")

command("qom-set", {"path": "/machine", "property": "imu-temperature-c",
                     "value": "40"})
if get("imu-temperature-c") != "40":
    raise RuntimeError("runtime temperature update was not retained")

command("quit")
qmp.close()
print("RESULT: BMI088 drift machine properties smoke passed")
PY
