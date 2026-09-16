#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
engine=null

for ((index = 1; index <= $#; index++)); do
    argument=${!index}
    if [[ "$argument" == "--engine" && $((index + 1)) -le $# ]]; then
        next=$((index + 1))
        engine=${!next}
    elif [[ "$argument" == --engine=* ]]; then
        engine=${argument#--engine=}
    fi
done

if [[ "$engine" == "ros2" ]]; then
    # ROS2 Python packages are supplied by the sourced ROS installation, not
    # by the uv project environment. The caller should source /opt/ros/... .
    exec python3 "$root_dir/tools/dm_mc02_sim_worker.py" "$@"
fi

uv_args=(run --project "$root_dir")
if [[ "$engine" == "mujoco" ]]; then
    uv_args+=(--extra mujoco)
fi

exec uv "${uv_args[@]}" python \
    "$root_dir/tools/dm_mc02_sim_worker.py" "$@"
