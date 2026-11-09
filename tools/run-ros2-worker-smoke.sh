#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
exec "${PYTHON:-python3}" "$script_dir/native_worker_smoke.py" \
    --sim "${SIM_EXECUTABLE:-"$root_dir/target/release/sim"}" --engine ros2
