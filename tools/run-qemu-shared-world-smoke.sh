#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
scene_python=${PYTHON:-python3}
mode=normal
scene_args=()
for argument in "$@"; do
    case "$argument" in
        --peer-exit|--reset|--upper-exit|--interrupt) mode=${argument#--} ;;
        --multi-cut|--can-deadlines|--cli) scene_args+=("$argument") ;;
        *) printf 'usage: %s [--multi-cut|--can-deadlines] [--cli] [--peer-exit|--reset|--upper-exit|--interrupt]\n' "$0" >&2; exit 2 ;;
    esac
done
[[ -x "$qemu_bin" ]] || { printf 'BLOCKED: QEMU not found: %s\n' "$qemu_bin" >&2; exit 77; }
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'BLOCKED: arm-none-eabi-gcc required' >&2; exit 77; }
"$scene_python" -c 'import mujoco,crccheck,yaml,qemu.qmp' || { printf '%s\n' 'BLOCKED: native Python dependencies required' >&2; exit 77; }
run_dir=$(mktemp -d '/tmp/dm-qemu.shared-world.XXXXXX')
trap 'rm -rf -- "$run_dir"' EXIT
for peer in 0 1; do
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
        -fno-stack-protector -nostdlib -nostartfiles -Wall -Wextra -Werror \
        -Wl,--gc-sections -Wl,--build-id=none -DPEER="$peer" -DTWO_CHASSIS=1 -DARENA_RUNTIME=1 \
        -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
        -o "$run_dir/peer$peer.elf" "$root_dir/smoke/dm_mc02_shared_world_smoke.c"
done
cd -- "$root_dir"
status=0
"$scene_python" "$script_dir/dm_mc02_scene_smoke.py" \
    --qemu "$qemu_bin" --firmware "$run_dir/peer0.elf" "$run_dir/peer1.elf" \
    --mode "$mode" --result "$run_dir/result.json" "${scene_args[@]}" || status=$?
if [[ -f "$run_dir/result.json" ]]; then
    result_json=$(cat "$run_dir/result.json")
    printf 'RESULT_JSON: %s\n' "$result_json"
fi
exit "$status"
