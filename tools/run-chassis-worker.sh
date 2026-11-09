#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
model=(--mujoco-model "$root_dir/models/four_wheel.xml")
for argument in "$@"; do
    if [[ "$argument" == --mujoco-model || "$argument" == --mujoco-model=* ]]; then
        model=()
        break
    fi
done
exec "$script_dir/run-worker.sh" --can-profile four-wheel "${model[@]}" "$@"
