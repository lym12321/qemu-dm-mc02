#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 77
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2
    exit 77
}

python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --socket cosim \
    --python-arg "$root_dir" --python-arg "{cosim}" -- \
    "$qemu_bin" -machine dm-mc02 -nodefaults -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim <<'PY'
import subprocess
import sys

root, socket_path = sys.argv[1:]
worker = [
    root + "/tools/run-worker.sh",
    "--cosim", socket_path,
    "--protocol", "v2",
    "--rate", "1000",
    "--frames", "16",
    "--adc-input", "4:4660",
    "--adc-voltage", "19:1.25",
]
process = subprocess.Popen(worker, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
stdout, stderr = process.communicate(timeout=10)
if process.returncode != 0:
    raise RuntimeError("v2 worker failed: %s\n%s" % (stdout, stderr))
print("RESULT: QEMU v2 RESET/STEP/ACK smoke passed")
PY
