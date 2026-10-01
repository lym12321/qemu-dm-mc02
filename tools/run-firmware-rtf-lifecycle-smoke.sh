#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required for the firmware RTF lifecycle smoke' >&2
    exit 1
}

run_dir=$(mktemp -d "/tmp/dm-qemu.dm-mc02-firmware-rtf-lifecycle.XXXXXX")
guest_bin="$run_dir/idle.bin"
qemu_wrapper="$run_dir/qemu-wrapper.sh"
exit_wrapper="$run_dir/exit-wrapper.sh"
normal_pid_file="$run_dir/normal.pid"
stalled_pid_file="$run_dir/stalled.pid"
exit_pid_file="$run_dir/exit.pid"
recorded_pid=''
recorded_starttime=''

read_process_identity() {
    local pid_file=$1
    local extra=''
    [[ -f "$pid_file" ]] || return 1
    IFS=' ' read -r recorded_pid recorded_starttime extra <"$pid_file" || return 1
    [[ "$recorded_pid" =~ ^[0-9]+$ ]] || return 1
    [[ "$recorded_starttime" =~ ^[0-9]+$ ]] || return 1
    [[ -z "$extra" ]]
}

recorded_process_alive() {
    local pid_file=$1
    local process_stat
    local process_fields
    local current_starttime
    read_process_identity "$pid_file" || return 1
    kill -0 "$recorded_pid" 2>/dev/null || return 1
    [[ -r "/proc/$recorded_pid/stat" ]] || return 1
    process_stat=$(<"/proc/$recorded_pid/stat")
    process_stat=${process_stat##*) }
    read -r -a process_fields <<<"$process_stat"
    ((${#process_fields[@]} >= 20)) || return 1
    current_starttime=${process_fields[19]}
    [[ "$current_starttime" == "$recorded_starttime" ]]
}

terminate_recorded_process() {
    local pid_file=$1
    recorded_process_alive "$pid_file" || return 0
    kill -TERM "$recorded_pid" 2>/dev/null || true
    for _ in {1..200}; do
        recorded_process_alive "$pid_file" || return 0
        sleep 0.01
    done
    recorded_process_alive "$pid_file" || return 0
    kill -KILL "$recorded_pid" 2>/dev/null || true
}

cleanup() {
    local status=$?
    trap - EXIT
    terminate_recorded_process "$normal_pid_file"
    terminate_recorded_process "$stalled_pid_file"
    terminate_recorded_process "$exit_pid_file"
    rm -rf -- "$run_dir"
    exit "$status"
}
trap cleanup EXIT

assert_process_gone() {
    local pid_file=$1
    local phase=$2
    read_process_identity "$pid_file" || {
        printf 'RESULT: failed (%s recorded an invalid process identity)\n' \
            "$phase" >&2
        exit 1
    }
    if recorded_process_alive "$pid_file"; then
        printf 'RESULT: failed (%s left process %s alive)\n' \
            "$phase" "$recorded_pid" >&2
        exit 1
    fi
}

# Cortex-M vector table followed by `wfi; b .` provides a live guest whose
# xTickCount fixture remains frozen at zero.
printf '\x00\x00\x02\x20\x09\x00\x00\x08\x30\xbf\xfe\xe7' >"$guest_bin"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -Eeuo pipefail' \
    'process_stat=$(</proc/$$/stat)' \
    'process_stat=${process_stat##*) }' \
    'read -r -a process_fields <<<"$process_stat"' \
    'printf "%s %s\n" "$$" "${process_fields[19]}" >"$DM_MC02_LIFECYCLE_PID_FILE"' \
    'exec "$DM_MC02_LIFECYCLE_QEMU" "$@"' >"$qemu_wrapper"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -Eeuo pipefail' \
    'process_stat=$(</proc/$$/stat)' \
    'process_stat=${process_stat##*) }' \
    'read -r -a process_fields <<<"$process_stat"' \
    'printf "%s %s\n" "$$" "${process_fields[19]}" >"$DM_MC02_LIFECYCLE_PID_FILE"' \
    'printf "%s\n" "controlled early exit" >&2' \
    'exit 17' >"$exit_wrapper"
chmod +x "$qemu_wrapper" "$exit_wrapper"

DM_MC02_LIFECYCLE_QEMU="$qemu_bin" \
DM_MC02_LIFECYCLE_PID_FILE="$normal_pid_file" \
python3 "$script_dir/dm_mc02_firmware_rtf.py" \
    --qemu "$qemu_wrapper" \
    --elf "$guest_bin" \
    --tick-address 0x20000000 \
    --warmup 0 \
    --duration 0.02 \
    --poll-interval 0.005 \
    --startup-timeout 1 \
    >"$run_dir/normal.log" 2>&1
assert_process_gone "$normal_pid_file" "normal collector exit"

set +e
DM_MC02_LIFECYCLE_QEMU="$qemu_bin" \
DM_MC02_LIFECYCLE_PID_FILE="$stalled_pid_file" \
python3 "$script_dir/dm_mc02_firmware_rtf.py" \
    --qemu "$qemu_wrapper" \
    --elf "$guest_bin" \
    --tick-address 0x20000000 \
    --virtual-seconds 0.001 \
    --ready-tick 1 \
    --poll-interval 0.02 \
    --startup-timeout 0.10 \
    >"$run_dir/stalled.log" 2>&1
stalled_status=$?
set -e
[[ "$stalled_status" -eq 1 ]] || {
    printf 'RESULT: failed (stalled collector exit=%s)\n' "$stalled_status" >&2
    sed -n '1,80p' "$run_dir/stalled.log" >&2
    exit 1
}
grep -Fq 'startup-ready exceeded startup timeout' "$run_dir/stalled.log" || {
    printf '%s\n' 'RESULT: failed (stalled collector gave the wrong diagnostic)' >&2
    sed -n '1,80p' "$run_dir/stalled.log" >&2
    exit 1
}
assert_process_gone "$stalled_pid_file" "startup-ready timeout"

set +e
DM_MC02_LIFECYCLE_PID_FILE="$exit_pid_file" \
python3 "$script_dir/dm_mc02_firmware_rtf.py" \
    --qemu "$exit_wrapper" \
    --elf "$guest_bin" \
    --tick-address 0x20000000 \
    --startup-timeout 0.50 \
    >"$run_dir/early-exit.log" 2>&1
exit_status=$?
set -e
[[ "$exit_status" -eq 1 ]] || {
    printf 'RESULT: failed (early-exit collector status=%s)\n' "$exit_status" >&2
    sed -n '1,80p' "$run_dir/early-exit.log" >&2
    exit 1
}
grep -Fq 'QEMU exited during startup with status 17: controlled early exit' \
    "$run_dir/early-exit.log" || {
    printf '%s\n' 'RESULT: failed (early-exit diagnostic omitted stderr)' >&2
    sed -n '1,80p' "$run_dir/early-exit.log" >&2
    exit 1
}
assert_process_gone "$exit_pid_file" "early process exit"

printf '%s\n' 'RESULT: firmware RTF startup deadline and cleanup passed'
printf '%s\n' '  normal WFI sample exited without a residual process'
printf '%s\n' '  frozen startup-ready path timed out and terminated QEMU'
printf '%s\n' '  early exit preserved bounded stderr diagnostics'
