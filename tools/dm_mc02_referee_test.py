#!/usr/bin/env python3
"""Actual trobot referee UART/DMA/RTOS consumer of the shared host endpoint.

This fixed fixture controls socket short writes, not OS backpressure or UART
arrival time. Its explicit engine step does not synchronize the QEMU CPU.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from dm_mc02_firmware_rtf import deadline_qmp_call, read_iwdg_timeouts
from dm_mc02_qmp import QmpSession
from dm_mc02_test_harness import running_qemu
from dm_mc02_trobot_business import _identity
from sim.boards import QemuBoard
from sim.boards.launch import qemu_command, regular_file, resolve_qemu
from sim.core import Session, StepContext
from sim.devices.referee import (
    Buff, GameStatus, Hit, MatchDurations, PowerHeat, RefereeEngine, RefereeState,
    RefereeUartEndpoint, RobotStatus, ScriptEvent, Shot,
)
from sim.transports import StreamTransport, connect_unix

STAGES = (("referee.initial", 82, 0), ("referee.partial", 19, 82),
          ("referee.complete", 82, 1))
REQUIRED = {"control.rx", "reset.poweron", "bsp.init", "referee.executor",
            "referee.partial.stable", "referee.uart", "task.stack"}
REQUIRED.update(f"referee.{phase}.{group}" for phase in ("initial", "complete")
                for group in ("game", "robot", "power", "buff", "fresh"))
LIMITS = [
    "Fixed 2026 V1.1.0 four-command fixture; unsupported 0206 is not transmitted.",
    "Actual UART7/HAL RX DMA/BSP callback/high executor/current basic.cc/public state.",
    "Socket budget injects a real 19-byte write then the retained 63-byte suffix; no OS-backpressure claim.",
    "READY timestamps are post-read QMP upper observations, not byte-arrival times.",
    "Engine advances one explicit second independently of CPU time; no global lockstep or official command-rate claim.",
    "BSP initialization exercises register/ID setup, not physical IMU samples; plant.physics SKIP.",
    "Functional consumer gate; physical calibration and sustained performance unverified.",
]

# Independent fixed official packed layouts. No production encoder computes
# expected bytes. Actual firmware compares the same public fields independently.
PAYLOADS = (
    ("2101000807060504030201", "03026400c80003006400500005",
     "000000000000000028000a001400", "050900070b0d00fe"),
    ("3103000807060504030201", "03024600c80003006400780006",
     "000000000000000028000c001900", "08110013171d0080"),
)
COMMANDS = (0x0001, 0x0201, 0x0202, 0x0204)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def create_engine():
    initial = RefereeState(GameStatus(1, 2, 1, 0x0102030405060708),
                           RobotStatus(3, 2, 100, 200, 3, 100, 80, 5),
                           PowerHeat(40, 10, 20), Buff(5, 9, 7, 11, 13, 0xfe))
    events = tuple(ScriptEvent(500_000_000, value) for value in (
        Hit(30), Shot(5, 7), RobotStatus(3, 2, 70, 200, 3, 100, 120, 6),
        Buff(8, 17, 19, 23, 29, 0x80)))
    return RefereeEngine(initial, durations=MatchDurations(3, 300), events=events,
                         cooling_42mm_per_s=2)


def check_wire(wire, batch):
    """Check complete group boundaries and literal payloads, without CRC code."""
    if type(wire) is not bytes or len(wire) != 82 or batch not in (0, 1):
        raise ValueError("invalid fixed referee snapshot batch")
    frames = []
    cursor = 0
    for offset, (command, payload_hex) in enumerate(zip(COMMANDS, PAYLOADS[batch])):
        payload = bytes.fromhex(payload_hex)
        size = len(payload) + 9
        raw = wire[cursor:cursor + size]
        sequence = batch * 4 + offset
        if (len(raw) != size or raw[0] != 0xa5 or
                int.from_bytes(raw[1:3], "little") != len(payload) or
                raw[3] != sequence or int.from_bytes(raw[5:7], "little") != command or
                raw[7:-2] != payload):
            raise ValueError("referee batch differs from literal wire contract")
        frames.append(dict(command_id=command, sequence=sequence,
                           payload_hex=raw[7:-2].hex(), wire_hex=raw.hex()))
        cursor += size
    return frames


def check_case(row):
    status, name = row["status"], row["name"]
    actual, expected = row["actual"], row["expected"]
    if any(type(value) is not int or not 0 <= value <= 0xffffffff
           for value in (actual, expected)):
        raise ValueError("firmware report fields must be uint32")
    if status == "SKIP" and name == "plant.physics" and actual == expected == 0:
        return
    if status != "PASS" or name not in REQUIRED:
        raise ValueError("failed or unexpected firmware case: " + name)
    if name == "task.stack":
        ok = expected == 64 and actual >= 64
    elif name == "reset.poweron":
        ok = actual == expected == 0x800000
    else:
        wanted = 1 if name.endswith(".fresh") or name.endswith(".stable") else 0
        ok = actual == expected == wanted
    if not ok:
        raise ValueError("firmware report fields disagree: " + name)


def source_snapshot(elf, qemu, firmware):
    """Hash actual inputs, including the canonical current firmware parser."""
    paths = [Path(elf), Path(qemu), ROOT / "uv.lock", ROOT / "qemu.lock",
             ROOT / "pyproject.toml", Path(__file__).resolve(),
             ROOT / "tools/run-trobot-referee-test.sh"]
    paths += [ROOT / "tools" / name for name in (
        "dm_mc02_qmp.py", "dm_mc02_firmware_rtf.py", "dm_mc02_test_harness.py",
        "dm_mc02_trobot_business.py")]
    paths += sorted((ROOT / "sim").rglob("*.py"))
    paths += [firmware / name for name in (
        "CMakeLists.txt", "CMakePresets.json", "app/CMakeLists.txt",
        "app/selftest/main.cc", "app/selftest/referee.cc", "bsp/CMakeLists.txt",
        "bsp/test/core.c", "bsp/test/transport.c", "bsp/test/selftest.h",
        "bsp/src/bsp.c", "bsp/src/uart.c", "bsp/src/time.c", "bsp/src/sys.c",
        "bsp/src/imu.c", "bsp/include/bsp/uart.h", "components/robomaster/src/basic.cc",
        "components/robomaster/include/robomaster/robomaster.h",
        "components/robomaster/include/robomaster/def.h",
        "components/msg/src/msg.cc", "components/msg/include/msg/msg.h",
        "components/utils/src/crc.cc", "components/utils/include/utils/crc.h",
        "components/utils/include/utils/os.h", "Core/Inc/FreeRTOSConfig.h",
        "Core/Src/freertos.c", "Core/Src/usart.c", "Core/Src/dma.c",
        "Core/Src/stm32h7xx_it.c", "Core/Src/stm32h7xx_hal_msp.c",
        "Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart.c",
        "Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma.c",
        "cmake/gcc-arm-none-eabi.cmake")]
    result = {}
    for path in paths:
        with path.open("rb") as stream:
            result[str(path.resolve())] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def checkout_snapshot(firmware):
    result = {}
    for name, path in (("project", ROOT), ("qemu", ROOT / "qemu/upstream"),
                       ("firmware", firmware),
                       ("firmware_robomaster", firmware / "components/robomaster"),
                       ("firmware_msg", firmware / "components/msg"),
                       ("firmware_utils", firmware / "components/utils")):
        values = {}
        head = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                              cwd=path, capture_output=True, text=True, timeout=2)
        if head.returncode not in (0, 1):
            raise RuntimeError("cannot observe checkout HEAD: " + name)
        values["head"] = head.stdout.strip() if head.returncode == 0 else None
        status = subprocess.check_output(["git", "status", "--short"],
                                         cwd=path, text=True, timeout=2)
        if len(status.encode()) > 12000:
            raise BufferError("checkout observation exceeds bound: " + name)
        values["status"] = status.strip()
        if values["head"] is None:
            values["head_unavailable"] = "repository has no commit; input files are hashed"
        values["declaration"] = "actual hashed working tree; no clean-checkout claim"
        result[name] = values
    return result


class SendBudget:
    """Delegate actual socket writes; own only test budget and observations."""

    def __init__(self, sock):
        self.sock = sock
        self.budget = None
        self.sent = bytearray()
        self.writes = []

    def send(self, data):
        if self.budget == 0:
            raise BlockingIOError
        count = len(data) if self.budget is None else min(self.budget, len(data))
        written = self.sock.send(data[:count])
        if len(self.writes) >= 164 or len(self.sent) + written > 164:
            raise BufferError("referee write observation exceeds fixed fixture")
        self.sent.extend(data[:written])
        self.writes.append(dict(bytes=written, wire_hex=bytes(data[:written]).hex(),
                                wall_time_ns=time.monotonic_ns()))
        if self.budget is not None:
            self.budget -= written
        return written


def remaining(owner, deadline):
    if owner.process.poll() is not None:
        raise RuntimeError(f"QEMU exited during referee test ({owner.process.returncode})")
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("referee consumer host deadline expired")
    return value


def drain(owner, deadline, endpoint, sock, *, budget=None):
    while True:
        remaining(owner, deadline)
        completed = endpoint.flush()
        remaining(owner, deadline)
        if completed or (budget is not None and budget.budget == 0):
            return
        select.select([], [sock], [], min(.01, remaining(owner, deadline)))
        remaining(owner, deadline)


def consume(owner, report, engine, resources):
    startup = owner.start_ns / 1e9 + 10
    deadline = owner.start_ns / 1e9 + 20
    report["qemu_start_monotonic_ns"] = owner.start_ns
    report["processes"] = {"qemu": _identity(owner.process.pid)}
    if report["processes"]["qemu"] is None:
        raise RuntimeError("QEMU identity missing at acquisition")
    timeout = min(2, remaining(owner, startup))
    qmp = resources["QMP"] = QmpSession(str(owner.sockets["qmp"]), timeout=timeout,
                                        connect_timeout=timeout, close_timeout=2)
    remaining(owner, startup)
    uart1 = resources["UART1"] = connect_unix(str(owner.sockets["uart1"]),
                                             min(2, remaining(owner, startup)))
    remaining(owner, startup)
    uart7 = resources["UART7"] = connect_unix(str(owner.sockets["uart7"]),
                                             min(2, remaining(owner, startup)))
    remaining(owner, startup)
    control = StreamTransport(uart1, max_pending=1, queue_name="referee control")
    budget = SendBudget(uart7)
    report["send_writes"] = budget.writes
    stream = StreamTransport(budget, max_pending=1, queue_name="referee snapshots")
    endpoint = resources["endpoint"] = RefereeUartEndpoint(stream)
    board = QemuBoard(qmp)

    def call(callback, *, startup_phase=False):
        bound = startup if startup_phase else deadline
        remaining(owner, bound)
        result = deadline_qmp_call(qmp, bound, "referee consumer", callback)
        remaining(owner, bound)
        return result

    def ack(bound):
        remaining(owner, bound)
        control.send(b"\xa5")
        while not control.flush():
            select.select([], [uart1], [], min(.01, remaining(owner, bound)))
        remaining(owner, bound)

    call(board.resume, startup_phase=True)
    buffer = bytearray()
    bytes_read = 0
    pending_wire = None
    while True:
        startup_phase = not report["stages"]
        bound = startup if startup_phase else deadline
        readable, _, _ = select.select([uart1], [], [], min(.01, remaining(owner, bound)))
        remaining(owner, bound)
        if owner.stderr_path.stat().st_size > 128000:
            raise BufferError("QEMU stderr exceeds fixed consumer diagnostic bound")
        if not readable:
            continue
        read_before = time.monotonic_ns()
        chunk = uart1.recv(4096)
        read_after = time.monotonic_ns()
        remaining(owner, bound)
        if not chunk:
            raise ConnectionError("firmware control UART disconnected")
        bytes_read += len(chunk)
        if bytes_read > 128000:
            raise BufferError("firmware UART output exceeds fixed consumer bound")
        buffer.extend(chunk)
        if len(buffer) > 12000:
            raise BufferError("firmware UART line buffer exceeds bound")
        while b"\n" in buffer:
            raw, _, suffix = buffer.partition(b"\n")
            buffer[:] = suffix
            line = raw.decode("ascii").strip()
            if not line.startswith("TTEST "):
                continue
            if len(report["uart_lines"]) >= 128:
                raise BufferError("firmware report capacity exhausted")
            report["uart_lines"].append(line)
            print(line, flush=True)
            fields = line.split()
            if len(fields) != 5:
                raise ValueError("invalid firmware report: " + line)
            if fields[1] in ("PHASE", "DONE"):
                report["summaries"].append(dict(kind=fields[1], values=list(map(int, fields[2:]))))
                if fields[1] == "DONE":
                    passes = Counter(r["name"] for r in report["cases"] if r["status"] == "PASS")
                    skips = [r["name"] for r in report["cases"] if r["status"] == "SKIP"]
                    report["missing"] = sorted(REQUIRED - passes.keys())
                    if (passes != Counter({name: 1 for name in REQUIRED}) or
                            skips != ["plant.physics"] or len(report["stages"]) != len(STAGES)):
                        raise ValueError("missing/duplicate/unexpected firmware coverage or stages")
                    expected = [len(REQUIRED), 0, 1]
                    if report["summaries"] != [dict(kind=k, values=expected) for k in ("PHASE", "DONE")]:
                        raise ValueError("firmware summary disagrees with actual reports")
                    call(board.pause)
                    timeouts, watchdog = call(lambda: read_iwdg_timeouts(qmp))
                    report["watchdog"] = watchdog
                    if timeouts or "window-violations=0" not in watchdog:
                        raise RuntimeError("watchdog event during referee consumer")
                    report["send_writes"] = budget.writes
                    report["wire_sent_hex"] = bytes(budget.sent).hex()
                    report["next_sequence"] = endpoint.next_sequence
                    if len(budget.sent) != 164 or endpoint.next_sequence != 8 or stream.tx_pending:
                        raise ValueError("incomplete fixed referee wire output")
                    remaining(owner, deadline)
                    return
                continue
            status, name = fields[1:3]
            actual, expected = map(int, fields[3:])
            row = dict(status=status, name=name, actual=actual, expected=expected,
                       read_before_ns=read_before, read_after_ns=read_after)
            report["cases"].append(row)
            if status != "READY":
                try:
                    check_case(row)
                except ValueError as exc:
                    report["errors"].append(str(exc))
                continue
            index = len(report["stages"])
            if index >= len(STAGES) or (name, actual, expected) != STAGES[index]:
                raise ValueError("unexpected actual referee stage: " + line)
            snapshot = call(board.snapshot, startup_phase=index == 0)
            row["qemu_observation_ns"] = snapshot["timestamp_ns"]
            if (report["stages"] and row["qemu_observation_ns"] <
                    report["stages"][-1]["qemu_observation_ns"]):
                raise RuntimeError("QEMU virtual observation time moved backwards")
            report["stages"].append(row)
            before = len(budget.sent)
            if index == 0:
                origin = snapshot["timestamp_ns"]
                engine.reset(Session(1, origin))
                report["engine_origin_ns"] = origin
                wire = endpoint.send_state(engine.get_state())
                frames = check_wire(wire, 0)
                report["batches"].append(dict(wire_hex=wire.hex(), frames=frames,
                                               engine_target_ns=engine.t_sim_ns))
                drain(owner, bound, endpoint, uart7)
                if bytes(budget.sent) != wire or stream.tx_pending:
                    raise ValueError("initial referee batch did not drain exactly")
            elif index == 1:
                target = report["engine_origin_ns"] + 1_000_000_000
                result = engine.step(StepContext(1, 1, target, 1_000_000_000))
                if result.status != "ok" or result.reached_time_ns != target:
                    raise ValueError("referee engine did not reach explicit target")
                budget.budget = 19
                pending_wire = endpoint.send_state(engine.get_state())
                frames = check_wire(pending_wire, 1)
                report["batches"].append(dict(wire_hex=pending_wire.hex(), frames=frames,
                                               engine_target_ns=engine.t_sim_ns))
                drain(owner, bound, endpoint, uart7, budget=budget)
                if (len(budget.sent) - before != 19 or bytes(budget.sent[before:]) != pending_wire[:19] or
                        len(stream.tx_queue) != 1 or bytes(stream.tx_queue[0]) != pending_wire[19:] or
                        stream.tx_queue[0].obj is not pending_wire):
                    raise ValueError("partial stage did not retain the original exact 63-byte suffix")
                row["queued_suffix_hex"] = bytes(stream.tx_queue[0]).hex()
                row["queued_suffix_bytes"] = len(stream.tx_queue[0])
            else:
                if pending_wire is None or bytes(stream.tx_queue[0]) != pending_wire[19:]:
                    raise ValueError("original referee suffix missing before completion")
                budget.budget = None
                drain(owner, bound, endpoint, uart7)
                if len(budget.sent) - before != 63 or bytes(budget.sent[before:]) != pending_wire[19:]:
                    raise ValueError("completion did not send exactly the original suffix")
            row["actual_sent_bytes"] = len(budget.sent) - before
            row["next_sequence"] = endpoint.next_sequence
            report["send_writes"] = budget.writes
            report["wire_sent_hex"] = bytes(budget.sent).hex()
            ack(bound)
            row["ack_wall_time_ns"] = time.monotonic_ns()
            remaining(owner, bound)


def run(elf, output, firmware, *, qemu=None):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("refusing to overwrite existing referee result directory: " + str(output))
    report = dict(result="failed", errors=[], notes=[], cleanup_errors=[], cases=[], stages=[],
                  summaries=[], missing=sorted(REQUIRED), uart_lines=[], batches=[], limits=LIMITS)
    output.mkdir(parents=True)
    write_json(output / "report.json", report)
    owner = None
    primary = None
    resources = {}
    previous_signal = signal.getsignal(signal.SIGTERM)

    def interrupted(sig, _frame):
        raise RuntimeError(f"referee consumer interrupted by signal {sig}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        elf = regular_file(elf, "RefereeTest ELF")
        qemu = resolve_qemu(qemu)
        firmware = Path(firmware).resolve()
        if not firmware.is_dir():
            raise ValueError("firmware source tree is not a directory: " + str(firmware))
        engine = resources["engine"] = create_engine()
        report["inputs_before"] = source_snapshot(elf, qemu, firmware)
        report["input_checkout"] = checkout_snapshot(firmware)
        command = qemu_command(elf, qemu, ("qmp", "uart1", "uart7"))
        command[command.index("-machine") + 1] += ",iwdg-boot-grace-ms=0"
        report["qemu_command_template"] = command
        with running_qemu(command, sockets=("qmp", "uart1", "uart7"), startup_timeout=10) as owner:
            try:
                consume(owner, report, engine, resources)
            except BaseException as exc:
                primary = exc
                raise
            finally:
                cleanup = []
                for name in ("endpoint", "engine", "UART7", "UART1", "QMP"):
                    resource = resources.pop(name, None)
                    if resource is not None:
                        try:
                            resource.close()
                        except BaseException as exc:
                            cleanup.append((name, exc))
                            report["cleanup_errors"].append(f"{name}: {exc}")
                try:
                    with owner.stderr_path.open("rb") as stream:
                        stream.seek(max(0, owner.stderr_path.stat().st_size - 12000))
                        report["qemu_stderr_tail"] = stream.read(12000).decode(errors="replace")
                except BaseException as exc:
                    cleanup.append(("stderr observation", exc))
                    report["cleanup_errors"].append(f"stderr observation: {exc}")
                if cleanup:
                    if primary is None:
                        primary = cleanup[0][1]
                    for name, exc in cleanup:
                        primary.add_note(f"{name} cleanup failed: {exc}")
                    raise primary
    except BaseException as exc:
        primary = exc
        report["errors"].append(str(exc) or type(exc).__name__)
    finally:
        # Pure admission may fail before the harness takes over resource scope.
        for name, resource in resources.items():
            try:
                resource.close()
            except BaseException as exc:
                report["cleanup_errors"].append(f"{name}: {exc}")
                if primary is not None:
                    primary.add_note(f"{name} cleanup failed: {exc}")
        resources.clear()
        report["wire_sent_hex"] = "".join(row["wire_hex"] for row in report.get("send_writes", []))
        report["missing"] = sorted(REQUIRED - {
            row["name"] for row in report["cases"] if row["status"] == "PASS"})
        report["skips"] = [row["name"] for row in report["cases"] if row["status"] == "SKIP"]
        report["missing_skips"] = [] if "plant.physics" in report["skips"] else ["plant.physics"]
        if report["missing"] or report["missing_skips"]:
            report["errors"].append("firmware coverage incomplete; see missing and missing_skips")
        identities = report.get("processes", {})
        try:
            report["qemu_reaped"] = owner is not None and owner.process.returncode is not None
            report["cleanup_verified"] = (set(identities) == {"qemu"} and report["qemu_reaped"] and
                all(identity is not None and _identity(identity["pid"]) != identity
                    for identity in identities.values()))
            if not report["cleanup_verified"]:
                report["errors"].append("QEMU identity unavailable or remains after harness cleanup")
        except BaseException as exc:
            report["errors"].append("cannot verify QEMU cleanup: " + str(exc))
        try:
            before = report.get("inputs_before")
            if before is None:
                raise ValueError("pre-run input hashes unavailable")
            report["inputs_after"] = source_snapshot(elf, qemu, firmware)
            if before != report["inputs_after"]:
                raise ValueError("input sources changed through harness cleanup")
        except BaseException as exc:
            report["errors"].append("final input verification failed: " + str(exc))
        if primary is not None:
            report["notes"] = list(getattr(primary, "__notes__", ()))
        signal.signal(signal.SIGTERM, previous_signal)
        report["result"] = "passed" if not report["errors"] and not report["cleanup_errors"] else "failed"
        write_json(output / "report.json", report)
    plant = "SKIP" if "plant.physics" in report["skips"] else "not observed"
    print("RESULT: trobot referee UART consumer " + report["result"] + "; plant.physics " + plant)
    if primary is not None:
        print(f"referee consumer failed: {primary}", file=sys.stderr)
        for note in report["notes"]:
            print(note, file=sys.stderr)
    return 0 if report["result"] == "passed" else 130 if isinstance(primary, KeyboardInterrupt) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf")
    parser.add_argument("output", nargs="?", default=ROOT / "build/test-results/trobot-referee")
    parser.add_argument("firmware_source", nargs="?", default=ROOT.parent / "trobot")
    parser.add_argument("--qemu")
    args = parser.parse_args(argv)
    try:
        return run(args.elf, args.output, args.firmware_source, qemu=args.qemu)
    except (OSError, ValueError) as exc:
        print("referee consumer failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
