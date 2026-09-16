#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}

[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required for the firmware RTF reset smoke' >&2
    exit 1
}

run_dir=$(mktemp -d "$root_dir/output.dm-mc02-firmware-rtf-reset.XXXXXX")
qmp_socket="$run_dir/qmp.sock"
guest_bin="$run_dir/idle.bin"
qemu_pid=''

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
        kill -TERM "$qemu_pid" 2>/dev/null || true
        for _ in {1..200}; do
            kill -0 "$qemu_pid" 2>/dev/null || break
            sleep 0.01
        done
        if kill -0 "$qemu_pid" 2>/dev/null; then
            kill -KILL "$qemu_pid" 2>/dev/null || true
        fi
    fi
    if [[ -n "$qemu_pid" ]]; then
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$run_dir"
    exit "$status"
}
trap cleanup EXIT

# Cortex-M vector table followed by `wfi; b .` keeps the CPU alive after cont.
printf '\x00\x00\x02\x20\x09\x00\x00\x08\x30\xbf\xfe\xe7' >"$guest_bin"

"$qemu_bin" -machine dm-mc02 -kernel "$guest_bin" -nodefaults \
    -display none -monitor none \
    -serial none -S -qmp "unix:$qmp_socket,server=on,wait=off" \
    >/dev/null 2>"$run_dir/qemu.stderr" &
qemu_pid=$!

for _ in {1..500}; do
    [[ -S "$qmp_socket" ]] && break
    kill -0 "$qemu_pid" 2>/dev/null || break
    sleep 0.01
done
if [[ ! -S "$qmp_socket" ]]; then
    printf '%s\n' 'RESULT: blocked (dm-mc02 QEMU did not create its QMP socket)' >&2
    sed -n '1,80p' "$run_dir/qemu.stderr" >&2
    exit 1
fi

python3 - "$qmp_socket" <<'PY'
from __future__ import annotations

import sys

from dm_mc02_firmware_rtf import SampleInvalidError, TickEpochTracker
from dm_mc02_qmp import QmpSession


qmp = QmpSession(sys.argv[1], timeout=2.0)
try:
    launch_status = qmp.command("query-status")
    if (
        not isinstance(launch_status, dict)
        or launch_status.get("running") is not False
        or launch_status.get("status") != "prelaunch"
    ):
        raise RuntimeError(f"-S did not leave QEMU stopped: {launch_status!r}")

    qmp.command("cont")
    qmp.command("stop")
    paused_status = qmp.command("query-status")
    if (
        not isinstance(paused_status, dict)
        or paused_status.get("running") is not False
        or paused_status.get("status") != "paused"
    ):
        raise RuntimeError(f"QEMU did not enter paused state: {paused_status!r}")

    tracker = TickEpochTracker(initial_tick=0, initial_watchdog_timeouts=0)
    normal_events = qmp.get_events(wait=False)
    tracker.observe(
        0,
        events=normal_events,
        watchdog_timeouts=0,
        phase="real QMP normal sample",
    )
    qmp.command("cont")
    qmp.command("system_reset")

    # This reply is an ordering barrier: all events emitted by system_reset
    # have passed through the pinned QMP client's event router.
    final_status = qmp.command("query-status")
    events = qmp.get_events(wait=False)
    reset_events = [event for event in events if event.get("event") == "RESET"]
    if not reset_events:
        raise RuntimeError(f"system_reset produced no RESET event: {events!r}")
    reset_data = reset_events[-1].get("data")
    if (
        not isinstance(reset_data, dict)
        or reset_data.get("reason") != "host-qmp-system-reset"
    ):
        raise RuntimeError(
            f"system_reset produced an unexpected RESET event: {reset_events!r}"
        )

    try:
        tracker.observe(
            0,
            events=events,
            watchdog_timeouts=0,
            phase="real QMP reset smoke",
        )
    except SampleInvalidError as error:
        if "QMP RESET event" not in str(error):
            raise RuntimeError(
                f"tracker rejected the event batch unexpectedly: {error}"
            ) from error
    else:
        raise RuntimeError("TickEpochTracker accepted an epoch containing RESET")

    if tracker.previous_tick != 0 or tracker.total_ticks != 0:
        raise RuntimeError("rejected RESET batch mutated tracker progress")
    print("RESULT: firmware RTF tracker rejected a real QMP RESET event")
    print(f"  normal sample boundary status: {paused_status.get('status')}")
    print(f"  normal event batch: {[event.get('event') for event in normal_events]}")
    print(f"  final QEMU status: {final_status.get('status')}")
    print(f"  event batch: {[event.get('event') for event in events]}")
finally:
    try:
        qmp.command("quit")
    except Exception:
        pass
    try:
        qmp.close()
    except Exception:
        pass
PY
