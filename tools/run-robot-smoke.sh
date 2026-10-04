#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ! python3 -c 'import mujoco' >/dev/null 2>&1; then
    printf '%s\n' 'skip: MuJoCo is required for Robot composition'
    exit 77
fi
exec "$script_dir/run-lab-channels-smoke.sh" --robot-only
