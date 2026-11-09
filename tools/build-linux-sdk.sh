#!/usr/bin/env bash
# Standard upstream SDK builds only. Run after installing ROS2 Jazzy dev packages.
set -Eeuo pipefail
root_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
sdk_root=${DM_NATIVE_SDK_ROOT:-"$root_dir/build/rust-gazebo-native"}
ros_prefix=${DM_ROS_PREFIX:-/opt/ros/jazzy}
mujoco_prefix=${DM_MUJOCO_PREFIX:-/opt/mujoco-3.12.0}
gazebo_commit=446a44335a45b704b4d36dabcc5508ee34eeb3d8
if [[ ! -d "$sdk_root/source/.git" ]]; then
    mkdir -p "$sdk_root"
    git clone --depth 1 --branch gz-sim8_8.15.0 https://github.com/gazebosim/gz-sim.git "$sdk_root/source"
fi
[[ $(git -C "$sdk_root/source" rev-parse HEAD) == "$gazebo_commit" ]]
git -C "$sdk_root/source" diff --exit-code --quiet HEAD
native_prefixes=("$ros_prefix"/opt/* "$ros_prefix")
cmake -S "$sdk_root/source" -B "$sdk_root/native-build-system" -G Ninja \
    -DCMAKE_INSTALL_PREFIX="$sdk_root/install" \
    -DCMAKE_PREFIX_PATH="$(IFS=';'; echo "${native_prefixes[*]}")" \
    -DCMAKE_IGNORE_PREFIX_PATH=/opt/anaconda3 -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_TESTING=OFF -DSKIP_PYBIND11=ON
cmake --build "$sdk_root/native-build-system" --parallel "${DM_BUILD_JOBS:-2}"
cmake --install "$sdk_root/native-build-system"
if [[ ! -f "$mujoco_prefix/lib/libmujoco.so.3.12.0" ]]; then
    archive="$sdk_root/mujoco-3.12.0-linux-x86_64.tar.gz"
    curl --fail --location --retry 3 \
        https://github.com/google-deepmind/mujoco/releases/download/3.12.0/mujoco-3.12.0-linux-x86_64.tar.gz \
        --output "$archive"
    sha256sum "$archive" > "$archive.sha256"
    mkdir -p "$mujoco_prefix"
    tar -xzf "$archive" --strip-components=1 -C "$mujoco_prefix"
fi
# Content pin is the actual native library from the admitted official release;
# the downloaded archive's separate hash is retained, not substituted for it.
printf '%s  %s\n' bd3f702ace8a31e1046f746880387858a981d11b01772a55ebef48ffd55ea5b8 \
    "$mujoco_prefix/lib/libmujoco.so.3.12.0" | sha256sum --check
if readelf -d "$sdk_root/install/lib/libgz-sim8.so.8.15.0" | grep -qi libpython; then
    printf '%s\n' 'Gazebo SDK unexpectedly links Python' >&2
    exit 1
fi
