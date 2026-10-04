#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
export PYTHONPATH="$root_dir:$root_dir/tools${PYTHONPATH:+:$PYTHONPATH}"
qemu_bin=${QEMU_SYSTEM_ARM:-"$root_dir/build/qemu/qemu-system-arm"}
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: arm-none-eabi-gcc is required' >&2; exit 1;
}
command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' 'blocked: python3 is required' >&2; exit 1;
}
[[ -x "$qemu_bin" ]] || {
    printf 'blocked: QEMU not found: %s\n' "$qemu_bin" >&2; exit 1;
}

run_dir=$(mktemp -d "/tmp/dm-qemu.lab-fixture.XXXXXX")
guest_elf="$run_dir/lab.elf"
trap 'rm -rf -- "$run_dir"' EXIT
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -O2 -Wall -Wextra -Werror \
    -ffreestanding -fno-builtin -fno-stack-protector -nostdlib -nostartfiles \
    -Wl,--gc-sections -Wl,--build-id=none \
    -Wl,-T,"$root_dir/smoke/dm_mc02_uart_smoke.ld" \
    -o "$guest_elf" "$root_dir/smoke/dm_mc02_lab_smoke.c"

# Slots: 0 co-sim, 1 USART1, 7 FDCAN1. The shared harness owns QEMU,
# subprocess cleanup and short private /tmp sockets; this consumer owns peers.
python3 "$script_dir/dm_mc02_test_harness.py" --timeout 20 \
    --expect-qemu-quit --socket cosim --socket uart --socket can --socket qmp \
    --python-arg "{cosim}" --python-arg "{uart}" --python-arg "{can}" \
    --python-arg "{qmp}" --python-arg "{qemu_start_ns}" -- \
    "$qemu_bin" -machine dm-mc02,fdcan-host-ack=on -kernel "$guest_elf" \
    -nodefaults -display none -monitor none -S \
    -chardev "socket,id=cosim,path={cosim},server=on,wait=off" \
    -serial chardev:cosim \
    -chardev "socket,id=uart1,path={uart},server=on,wait=off" \
    -serial chardev:uart1 \
    -serial none -serial none -serial none -serial none -serial none \
    -chardev "socket,id=can1,path={can},server=on,wait=off" \
    -serial chardev:can1 -qmp "unix:{qmp},server=on,wait=off" <<'PY'
import select
import signal
import sys
import time

from dm_mc02_firmware_rtf import deadline_qmp_call
from dm_mc02_qmp import QmpSession
from dm_mc02_sim_worker import CanStream, FramedSocket, TELEMETRY
from sim.boards import QemuBoard
from sim.boards.channels import LabChannels
from sim.fabric import CanFrame
from sim.transports import StreamTransport, connect_unix

cosim_path, uart_path, can_path, qmp_path, start_ns = sys.argv[1:]
started = int(start_ns) / 1_000_000_000
deadline = started + 10.0
peers = []
qmp = None
primary = None
lines = []
can_events = []
uart_tail = bytearray()
observation_batches = 0


def interrupted(signum, _frame):
    raise RuntimeError(f"Lab consumer interrupted by signal {signum}")


signal.signal(signal.SIGTERM, interrupted)


def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("Lab consumer deadline expired; UART=%r CAN=%r" %
                           (lines, can_events))
    return value


def call(callback):
    return deadline_qmp_call(qmp, deadline, "Lab consumer", callback)


try:
    for path in (cosim_path, uart_path, can_path):
        peers.append(connect_unix(path, min(2.0, remaining())))
    qmp = QmpSession(qmp_path, timeout=min(2.0, remaining()),
                     connect_timeout=min(2.0, remaining()))
    board = QemuBoard(qmp)
    cosim = FramedSocket(peers[0])
    uart = StreamTransport(peers[1])
    can = CanStream(peers[2])

    # Consume the real opening telemetry before admitting the explicit v1
    # RESET. Socket existence and QMP status are not application readiness.
    opened = False
    while not opened:
        remaining()
        for kind, sequence, stamp, payload in cosim.receive():
            if kind != TELEMETRY or len(payload) != 12:
                raise RuntimeError("unexpected co-sim opening frame")
            opened = True
        if not opened:
            select.select([peers[0]], [], [], min(.01, remaining()))
    channels = LabChannels(uarts={"uart1": uart}, cans={"can1": can}, cosim=cosim)
    reset_time = call(board.snapshot)["timestamp_ns"]
    channels.reset(reset_time)
    call(board.resume)

    def pump():
        global observation_batches
        remaining()
        before = call(board.snapshot)
        batch = channels.service(before["timestamp_ns"])
        after = call(board.snapshot)
        assert after["timestamp_ns"] >= before["timestamp_ns"]
        observation_batches += 1
        for event in batch:
            if event.endpoint == "uart1":
                # UART event timestamps identify the caller's polling sample.
                # The independent snapshot after read is the actual upper bound.
                assert event.timestamp_ns == before["timestamp_ns"]
                assert event.timestamp_ns <= after["timestamp_ns"]
                uart_tail.extend(event.data)
                if len(uart_tail) > 4096:
                    raise BufferError("Lab UART record exceeded 4096 bytes")
                while b"\n" in uart_tail:
                    line, _, tail = uart_tail.partition(b"\n")
                    uart_tail[:] = tail
                    lines.append(line.decode("ascii"))
                    if len(lines) > 32:
                        raise BufferError("Lab UART reports exceeded 32 lines")
                    print("firmware:", lines[-1], flush=True)
            elif event.endpoint == "can1":
                assert isinstance(event.data, CanFrame)
                assert event.timestamp_ns == event.data.timestamp_ns
                assert event.timestamp_ns <= after["timestamp_ns"]
                can_events.append(event.data)
                if len(can_events) > 16:
                    raise BufferError("Lab CAN reports exceeded 16 frames")
            else:
                raise RuntimeError("unexpected Lab endpoint")
        return after

    def wait_line(expected, since=0):
        while expected not in lines[since:]:
            pump()
            select.select(peers, [], [], min(.01, remaining()))

    wait_line("LAB_READY")
    # Ready is the fixture's actual USART event. Runtime also has one absolute
    # deadline; QMP calls must not extend it with per-call waits.
    deadline = started + 15.0
    wait_line("ADC=43329")  # Existing nominal 24 V board-divider profile.
    wait_line("KEY=1")
    state = call(board.snapshot)
    assert state["key"] == {"pin": "A15", "pressed": False, "level": 1}
    assert state["led"] == {
        "pin": "A7", "rgb": 0, "brightness": 0, "source": "gpio-latch"}

    channels.send_uart("uart1", b"q")
    wait_line("UART=q")
    cursor = len(lines)
    call(lambda: board.set_key(True))
    wait_line("KEY=0", cursor)
    state = call(board.snapshot)
    assert state["key"]["pressed"] is True and board.digital("key", state) == 0
    assert state["led"] == {
        "pin": "A7", "rgb": 0xffffff, "brightness": 255, "source": "gpio-latch"}
    cursor = len(lines)
    call(lambda: board.set_key(False))
    wait_line("KEY=1", cursor)
    state = call(board.snapshot)
    assert state["key"]["pressed"] is False and board.digital("led", state) == 0

    # Use observed QEMU time, waiting for real progress if a timestamp would
    # equal RESET/input. Never invent an increment to pass v1 admission.
    def next_time(previous):
        while True:
            state = pump()
            if state["timestamp_ns"] > previous:
                return state["timestamp_ns"]
            select.select(peers, [], [], min(.01, remaining()))

    stamp = next_time(reset_time)
    channels.send_adc(4, voltage_uv=1_650_000, timestamp_ns=stamp)
    wait_line("ADC=32768")  # Independent midpoint: round(65535 / 2).
    stamp = next_time(stamp)
    channels.send_adc(4, raw=12345, timestamp_ns=stamp)
    wait_line("ADC=12345")

    sent_time = pump()["timestamp_ns"]
    channels.send_can("can1", CanFrame(
        0x321, flags=0, dlc=8, data=b"ABCDEFGH", timestamp_ns=sent_time))
    while not can_events:
        pump()
        select.select(peers, [], [], min(.01, remaining()))
    assert len(can_events) == 1
    echoed = can_events[0]
    assert (echoed.can_id, echoed.flags, echoed.dlc, echoed.data) == (
        0x322, 0, 8, b"ABCDEFGH"), echoed
    assert sent_time <= echoed.timestamp_ns
    assert "CAN_BAD" not in lines
    call(board.pause)
    call(lambda: qmp.command("quit"))
except BaseException as exc:
    primary = exc
    raise
finally:
    failures = []
    # Every owned connection gets an independent attempt, including on failure
    # or interruption. The shared harness subsequently reaps QEMU/test groups.
    for name, resource in [("QMP", qmp), *list(zip(("co-sim", "UART", "CAN"), peers))]:
        if resource is not None:
            try:
                resource.close()
            except BaseException as exc:
                failures.append((name, exc))
    if failures:
        error = primary if primary is not None else failures[0][1]
        for name, failure in failures:
            error.add_note(f"Lab cleanup {name} failed: {failure}")
        if primary is None:
            raise error
print("Lab consumer observations:", observation_batches, "batches", flush=True)
PY
# The harness has checked QEMU quit and completed cleanup before announcing PASS.
printf '%s\n' 'RESULT: Lab UART/CAN/ADC and KEY→GPIO LED consumer passed'
