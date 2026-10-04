#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir:$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
chassis_python="$root_dir/.venv/bin/python"
[[ -x "$chassis_python" ]] || {
    printf '%s\n' 'Project Python missing: uv sync --locked --group dev --extra mujoco' >&2
    exit 1
}
[[ $# -ge 1 && $# -le 3 ]] || {
    printf '%s\n' 'Usage: run-trobot-chassis-test.sh ChassisTest.elf [output-directory [firmware-source]]' >&2
    exit 2
}
output=${2:-"$root_dir/build/test-results/trobot-chassis"}
firmware_source=${3:-"$root_dir/../trobot"}
"$chassis_python" "$script_dir/dm_mc02_chassis_test.py" --initialize "$output" "$1" "$qemu_bin"
harness_status=0
"$chassis_python" "$script_dir/dm_mc02_test_harness.py" --timeout 25 \
    --socket qmp --socket uart --socket cosim --socket can \
    --python-arg '{qmp}' --python-arg '{uart}' --python-arg '{cosim}' --python-arg '{can}' \
    --python-arg "$output" --python-arg "$1" --python-arg "$qemu_bin" \
    --python-arg "$firmware_source" --python-arg '{qemu_start_ns}' \
    --python-arg '{qemu_pid}' -- \
    "$qemu_bin" -machine dm-mc02,fdcan-host-ack=on,iwdg-boot-grace-ms=0 \
    -kernel "$1" -nodefaults -display none -monitor none -S \
    -qmp 'unix:{qmp},server=on,wait=off' \
    -chardev 'socket,id=cosim,path={cosim},server=on,wait=off' -serial chardev:cosim \
    -chardev 'socket,id=uart,path={uart},server=on,wait=off' -serial chardev:uart \
    -serial none -serial none -serial none -serial none -serial none \
    -chardev 'socket,id=can,path={can},server=on,wait=off' -serial chardev:can <<'PY' || harness_status=$?
from dm_mc02_chassis_test import main
raise SystemExit(main())
PY
"$chassis_python" "$script_dir/dm_mc02_chassis_test.py" --finalize "$output" "$harness_status"
