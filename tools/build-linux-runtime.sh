#!/usr/bin/env bash
# Build tools may use Python; the resulting runtime does not.
set -Eeuo pipefail
root_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
gazebo_prefix=${DM_GAZEBO_PREFIX:-"$root_dir/build/rust-gazebo-native/install"}
ros_prefix=${DM_ROS_PREFIX:-/opt/ros/jazzy}
mujoco_prefix=${DM_MUJOCO_PREFIX:-/opt/mujoco-3.12.0}
if [[ ! -f "$gazebo_prefix/lib/pkgconfig/gz-sim8.pc" ]]; then
    printf 'Missing Python-free Gazebo SDK: %s\n' "$gazebo_prefix" >&2
    exit 1
fi
native_libraries=("$gazebo_prefix/lib" "$ros_prefix/lib" "$mujoco_prefix/lib")
native_pkgconfig=("$gazebo_prefix/lib/pkgconfig")
for vendor_dir in "$ros_prefix"/opt/*; do
    [[ ! -d "$vendor_dir/lib" ]] || native_libraries+=("$vendor_dir/lib")
    [[ ! -d "$vendor_dir/lib/pkgconfig" ]] || native_pkgconfig+=("$vendor_dir/lib/pkgconfig")
done
export LD_LIBRARY_PATH="$(IFS=:; echo "${native_libraries[*]}")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PKG_CONFIG_PATH="$(IFS=:; echo "${native_pkgconfig[*]}")${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export CMAKE_PREFIX_PATH="$ros_prefix${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
export AMENT_PREFIX_PATH="$ros_prefix${AMENT_PREFIX_PATH:+:$AMENT_PREFIX_PATH}"
export PYTHONPATH="$ros_prefix/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export MUJOCO_DYNAMIC_LINK_DIR="$mujoco_prefix/lib"
cd "$root_dir"
cargo build --release --locked -p sim-host --features linux-runtime "$@"
