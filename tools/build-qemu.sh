#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
source_dir="$root_dir/qemu/upstream"
build_dir="$root_dir/build/qemu"
build_type=${QEMU_BUILD_TYPE:-release}
qom_cast_debug=${QEMU_QOM_CAST_DEBUG:-false}
plugins=${QEMU_TCG_PLUGINS:-false}
device_config=dm-mc02

qemu_configure_args=(
    --target-list=arm-softmmu
    --with-devices-arm="$device_config"
    --disable-docs
    --disable-werror
    --disable-capstone
    --disable-slirp
    --enable-fdt=internal
    --enable-download
    --prefix="$root_dir/build/install"
)

case "$build_type" in
    debug|debugoptimized|release|minsize|plain) ;;
    *)
        printf 'invalid QEMU_BUILD_TYPE: %s\n' "$build_type" >&2
        exit 2
        ;;
esac

if [[ ! -x "$source_dir/configure" ]]; then
    printf '%s\n' "QEMU source is missing: run git submodule update --init qemu/upstream" >&2
    exit 1
fi
[[ -x "$root_dir/.venv/bin/python" && -x "$root_dir/.venv/bin/ninja" ]] || {
    printf '%s\n' 'blocked: uv environment is missing; run uv sync --group dev' >&2
    exit 1
}
export PATH="$root_dir/.venv/bin:$PATH"
if ! rg -q "dm_mc02.c" "$source_dir/hw/arm/meson.build" || [[ ! -f "$source_dir/hw/arm/dm_mc02.c" ]]; then
    printf '%s\n' 'blocked: wrong QEMU source; restore the pinned qemu/upstream submodule' >&2
    exit 1
fi
mkdir -p "$build_dir"
if [[ ! -f "$build_dir/build.ninja" ]]; then
    (
        cd "$build_dir"
        "$source_dir/configure" "${qemu_configure_args[@]}"
    )
fi
# A build directory created before the project-specific device profile was
# introduced may still point at the generic ARM config.  Refresh only that
# stale configuration; ordinary source changes remain Ninja's responsibility.
device_cross="$build_dir/config-meson.cross"
if [[ -f "$build_dir/build.ninja" ]] &&
   ! rg -Fqx "arm-softmmu = '$device_config'" "$device_cross"; then
    (
        cd "$build_dir"
        "$source_dir/configure" "${qemu_configure_args[@]}"
    )
fi
# Existing build directories may have been created by an older version of
# this script with different Meson options.  Reconfigure only when the
# requested options differ or the caller explicitly asks for it; ordinary
# source changes are handled by Ninja's generated dependency graph.
meson_cmdline="$build_dir/meson-private/cmd_line.txt"
needs_reconfigure=0
if [[ ! -f "$meson_cmdline" || "${QEMU_RECONFIGURE:-0}" == 1 ]]; then
    needs_reconfigure=1
elif ! rg -Fqx "buildtype = $build_type" "$meson_cmdline" ||
     ! rg -Fqx "qom_cast_debug = $qom_cast_debug" "$meson_cmdline" ||
     ! rg -Fqx "plugins = $plugins" "$meson_cmdline" ||
     ! rg -Fqx 'werror = false' "$meson_cmdline"; then
    needs_reconfigure=1
fi
if ((needs_reconfigure)); then
    "$root_dir/tools/meson" setup "$build_dir" "$source_dir" --reconfigure \
        -Dbuildtype="$build_type" -Dqom_cast_debug="$qom_cast_debug" \
        -Dplugins="$plugins" -Dwerror=false >/dev/null
fi
"$root_dir/tools/meson" compile -C "$build_dir" qemu-system-arm
printf 'QEMU build ready: %s (buildtype=%s)\n' "$build_dir/qemu-system-arm" "$build_type"
