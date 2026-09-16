#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-}
if [[ -z "$qemu_bin" ]]; then
    # Performance measurements must use the current source build. Falling back
    # to another build directory can silently select a different machine model.
    qemu_bin="$root_dir/build/qemu/qemu-system-arm"
fi
elf=${DM_MC02_ELF:-"$root_dir/../trobot/build/Release/trobot.elf"}
warmup=0.25
duration=1.0
virtual_seconds=''
runs=1
min_rtf=''
ready_tick=1
poll_interval=0.250
iwdg_boot_grace_ms=''
adc_accurate_timing=0
tcg_tb_size=''
tcg_thread=''

usage() {
    printf 'usage: %s [--elf path] [--warmup seconds] [--duration seconds] [--virtual-seconds seconds] [--runs count] [--min-rtf factor] [--ready-tick ticks] [--poll-interval seconds] [--iwdg-boot-grace-ms ms] [--adc-accurate-timing] [--tcg-tb-size bytes] [--tcg-thread single|multi]\n' "$0"
}

while (($#)); do
    case "$1" in
        --elf) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; elf=$2; shift 2 ;;
        --warmup) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; warmup=$2; shift 2 ;;
        --duration) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; duration=$2; shift 2 ;;
        --virtual-seconds) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; virtual_seconds=$2; shift 2 ;;
        --runs) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; runs=$2; shift 2 ;;
        --min-rtf) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; min_rtf=$2; shift 2 ;;
        --ready-tick) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; ready_tick=$2; shift 2 ;;
        --poll-interval) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; poll_interval=$2; shift 2 ;;
        --iwdg-boot-grace-ms) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; iwdg_boot_grace_ms=$2; shift 2 ;;
        --adc-accurate-timing) adc_accurate_timing=1; shift ;;
        --tcg-tb-size) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; tcg_tb_size=$2; shift 2 ;;
        --tcg-thread) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; tcg_thread=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -x "$qemu_bin" ]] || { printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1; }
[[ -f "$elf" ]] || { printf 'blocked: firmware ELF not found: %s\n' "$elf" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'blocked: python3 is required' >&2; exit 1; }
command -v arm-none-eabi-nm >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-nm is required to locate xTickCount' >&2
    exit 1
}

tick_address=$(arm-none-eabi-nm -n "$elf" | awk '
    $3 == "xTickCount" && !found { print "0x" $1; found = 1 }
')
[[ -n "$tick_address" ]] || {
    printf 'blocked: xTickCount is not present in %s\n' "$elf" >&2
    exit 1
}

python_args=(
    "$script_dir/dm_mc02_firmware_rtf.py"
    --qemu "$qemu_bin"
    --elf "$elf"
    --tick-address "$tick_address"
    --warmup "$warmup"
    --duration "$duration"
    --runs "$runs"
    --ready-tick "$ready_tick"
    --poll-interval "$poll_interval"
)
[[ -n "$virtual_seconds" ]] && python_args+=(--virtual-seconds "$virtual_seconds")
[[ -n "$min_rtf" ]] && python_args+=(--min-rtf "$min_rtf")
[[ -n "$iwdg_boot_grace_ms" ]] && python_args+=(--iwdg-boot-grace-ms "$iwdg_boot_grace_ms")
((adc_accurate_timing == 0)) || python_args+=(--adc-accurate-timing)
[[ -n "$tcg_tb_size" ]] && python_args+=(--tcg-tb-size "$tcg_tb_size")
[[ -n "$tcg_thread" ]] && python_args+=(--tcg-thread "$tcg_thread")

exec python3 "${python_args[@]}"
