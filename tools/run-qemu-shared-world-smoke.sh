#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
mode=normal
if [[ $# -gt 0 ]]; then
    if [[ $# -ne 1 || ( $1 != --peer-exit && $1 != --reset ) ]]; then
        printf 'usage: %s [--peer-exit|--reset]\n' "$0" >&2
        exit 2
    fi
    mode=${1#--}
fi
[[ -x "$qemu_bin" ]] || { printf 'SKIP: QEMU not found: %s\n' "$qemu_bin" >&2; exit 77; }
command -v arm-none-eabi-gcc >/dev/null 2>&1 || { printf '%s\n' 'SKIP: arm-none-eabi-gcc required' >&2; exit 77; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'SKIP: python3 required' >&2; exit 77; }

run_dir=$(mktemp -d '/tmp/dm-qemu.shared-world.XXXXXX')
trap 'rm -rf -- "$run_dir"' EXIT
for peer in 0 1; do
    arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -ffreestanding -fno-builtin \
        -fno-stack-protector -nostdlib -nostartfiles -Wall -Wextra -Werror \
        -Wl,--gc-sections -Wl,--build-id=none -DPEER="$peer" \
        -Wl,-T,"$root_dir/smoke/dm_mc02_cosim_link_readback_smoke.ld" \
        -o "$run_dir/peer$peer.elf" "$root_dir/smoke/dm_mc02_shared_world_smoke.c"
done

# Native imports and the entrypoint must come from the selected checkout.
cd -- "$root_dir"
python3 "$script_dir/dm_mc02_shared_world_smoke.py" \
    --qemu "$qemu_bin" --firmware "$run_dir/peer0.elf" "$run_dir/peer1.elf" \
    --mode "$mode" --result "$run_dir/result.json"
result_json=$(cat "$run_dir/result.json")
rm -rf -- "$run_dir"
trap - EXIT
[[ ! -e "$run_dir" ]]
printf 'RESULT_JSON: %s\n' "$result_json"
printf '%s\n' 'RESULT: native dual-QEMU single-world CAN/BMI088 smoke passed'
