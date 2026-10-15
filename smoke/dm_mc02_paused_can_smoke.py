#!/usr/bin/env python3
"""Finite native consumer of paused CAN output, without a world/CPU barrier.

The unchanged tiny guest and RAM literals come from dm-mc02-cpu-test.c.
Qtest only sets up and observes that established fixture; native STTCG executes
it. All CAN reads belong to the original canonical CanStream.
"""

from collections import deque
import argparse
import hashlib
import json
import math
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from dm_mc02_firmware_rtf import deadline_qmp_call
from dm_mc02_qmp import QmpSession
from dm_mc02_test_harness import running_qemu
from qemu.machine.qtest import QEMUQtestProtocol
from sim.boards import drain_paused_can
from sim.boards.launch import qemu_command, resolve_qemu
from sim.transports.can_stream import CanStream
from sim.transports.unix import connect_unix


# Identical 116 bytes to execution_periodic_can_image; no compiler is needed.
GUEST = bytes.fromhex(
    "0020002041000008000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000049000008"
    "30bffde700000000074801210160022101600648016801310160032902d1044800"
    "210160704700bfd0a000400000002010e000e0")
GUEST_SHA = "b112037d5d68ee1d352aee00915f2ce1ded8ed82bdfd349634a21093248b8482"
TX_RAM = bytes.fromhex(
    "00000008000008001122334455667788"
    "0000fc07000008008877665544332211")
TIMESTAMPS = (1000003, 1000005, 2000003, 2000005, 3000003, 3000005)
TARGETS = (1000000, 2000000, 3000000, 3000200)
PAYLOADS = (bytes.fromhex("1122334455667788"),
            bytes.fromhex("8877665544332211"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def starttime(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def expected_wire(index, prefix_count):
    """Independent <IIB3xQ64s literals, including unused payload padding."""
    raw = bytearray(84)
    raw[8] = 8
    if index < prefix_count:
        raw[1] = 2
        raw[20:22] = index.to_bytes(2, "little")
        raw[22:24] = b"\xa5\x5a"
    else:
        cpu = index - prefix_count
        expect(0 <= cpu < 6, "unexpected extra native CAN record")
        raw[:2] = b"\x00\x02" if cpu % 2 == 0 else b"\xff\x01"
        raw[12:20] = TIMESTAMPS[cpu].to_bytes(8, "little")
        raw[20:28] = PAYLOADS[cpu % 2]
    return bytes(raw)


def run(binary, report):
    overall = time.monotonic() + 20.0
    deadline = overall
    owner = None
    peers = []
    peer_cleanup_errors = []
    primary = None
    image_dir = None
    sequence = 0
    trace = deque(maxlen=16)
    observed = {"wire_count": 0, "sink_count": 0, "read_calls": 0,
                "max_read_bytes": 0, "cpu_timestamps_ns": []}
    wire_hash = hashlib.sha256()
    last_tx = None
    empty_read = None
    phase = "startup"

    def event(kind, **fields):
        nonlocal sequence
        sequence += 1
        entry = {"sequence": sequence, "phase": phase, "kind": kind, **fields}
        trace.append(entry)
        return entry

    def check():
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError(f"native paused CAN {phase} deadline expired")
        if owner is not None and owner.process.poll() is not None:
            raise RuntimeError(f"native QEMU exited early ({owner.process.returncode})")
        return remaining

    def command(name, arguments=None):
        nonlocal last_tx
        check()
        result = deadline_qmp_call(
            qmp, deadline, f"paused CAN {phase}",
            lambda: qmp.command(name, arguments))
        check()
        if name == "qom-get" and arguments.get("property") == "fdcan-tx-state":
            tx = result["controllers"]["dm-mc02.fdcan1"]
            last_tx = event("tx", timestamp_ns=result["timestamp_ns"], **tx)
        return result

    def qom(path, prop):
        return command("qom-get", {"path": path, "property": prop})

    def control(text):
        check()
        qtest.settimeout(min(2.0, check()))
        response = qtest.cmd(text)
        check()
        fields = response.strip().split()
        expect(fields and fields[0] == "OK" and response.endswith("\n"),
               f"qtest {text!r}: {response!r}")
        return fields

    def read(address):
        response = control(f"readl {address:#x}")
        expect(len(response) == 2, f"invalid qtest read: {response!r}")
        return int(response[1], 16)

    def write(address, value):
        expect(control(f"writel {address:#x} {value:#x}") == ["OK"],
               "unexpected qtest write result")

    def memory(address, raw):
        expect(control(f"write {address:#x} {len(raw)} 0x{raw.hex()}") == ["OK"],
               "unexpected qtest memory-write result")

    def tx_state(stamp):
        state = qom("/machine", "fdcan-tx-state")
        expect(state["schema"] == 1 and state["timestamp_ns"] == stamp,
               f"unexpected native TX state: {state!r}")
        tx = state["controllers"]["dm-mc02.fdcan1"]
        expect(tx["tx_dropped"] == 0, f"native CAN drop: {tx!r}")
        return tx

    def stopped(stamp, ledger, status="paused"):
        execution = command("x-query-virtual-execution")
        expect(execution["current-ns"] == stamp, f"time changed: {execution!r}")
        if status == "paused":
            expect(execution["state"] == "reached" and
                   execution["target-ns"] == stamp, f"fence changed: {execution!r}")
        actual = command("query-status")
        expect(actual["running"] is False and actual["status"] == status,
               f"not stopped: {actual!r}")
        actual_ledger = read(0x20000000)
        expect(actual_ledger == ledger, "guest pair ledger changed")
        return {"execution": execution, "status": actual, "ledger": actual_ledger}

    def connected(value):
        while qom("/chardevs/can1", "connected") is not value:
            check()

    try:
        expect(len(GUEST) == 116 and hashlib.sha256(GUEST).hexdigest() == GUEST_SHA,
               "fixed periodic guest bytes changed")
        image_dir = tempfile.TemporaryDirectory(prefix="dm-qemu.paused-can.", dir="/tmp")
        report["image_directory"] = image_dir.name
        image = Path(image_dir.name) / "periodic.bin"
        image.write_bytes(GUEST)
        sockets = ("qmp", "can1", "qtest")
        argv = qemu_command(image, binary, sockets)
        argv[argv.index("-machine") + 1] += ",electrical-power=on"
        argv += ["-accel", "tcg,thread=single", "-icount", "shift=0,align=off,sleep=off",
                 "-qtest", "unix:{qtest},server=on,wait=off", "-qtest-log", "/dev/null"]
        with running_qemu(argv, sockets=sockets,
                          startup_timeout=min(3.0, check())) as owner:
            report["process"] = {
                "pid": owner.process.pid, "starttime": starttime(owner.process.pid),
                "socket_directory": str(owner.stderr_path.parent),
                "argv": [owner.expand(value) for value in argv],
                "executable": str(Path(f"/proc/{owner.process.pid}/exe").resolve()),
                "executable_sha256": sha(f"/proc/{owner.process.pid}/exe"),
            }
            expect(report["process"]["starttime"] is not None, "QEMU identity vanished")
            try:
                qmp = QmpSession(str(owner.sockets["qmp"]), timeout=min(2.0, check()),
                                 connect_timeout=min(2.0, check()))
                peers.append(("QMP", qmp))
                qtest = QEMUQtestProtocol(str(owner.sockets["qtest"]))
                peers.append(("qtest", qtest))
                qtest.settimeout(min(2.0, check()))
                qtest.connect()
                can_socket = connect_unix(str(owner.sockets["can1"]),
                                          min(2.0, check()))
                peers.append(("original CAN", can_socket))
                can = CanStream(can_socket)
                connected(True)
                stopped(0, 0, "prelaunch")

                phase = "setup"
                write(0x20000000, 0)
                write(0x580244e0, read(0x580244e0) | 4)
                write(0x58020800, (read(0x58020800) & ~(3 << 30)) | (1 << 30))
                write(0x58020818, 1 << 15)
                expect(read(0x58020814) & (1 << 15), "PC15 output was not asserted")
                expect(qom("/machine", "system-5v-good") is True, "5 V rail is not good")
                write(0x4000a018, 1)
                write(0x4000a0c0, 0x02000000)
                write(0x4000a0c8, 0)
                memory(0x4000ac00, TX_RAM)
                write(0x4000a018, 0)
                write(0x58024418, 0xa00)
                write(0xe000e014, 7999)
                write(0xe000e018, 0)
                write(0xe000e010, 7)
                expect(tx_state(0)["pending_frames"] == 0, "unexpected initial backlog")

                phase = "prefill"
                deadline = min(overall, time.monotonic() + 2.0)
                submitted = 0
                while submitted < 65536:
                    raw = (bytes.fromhex("0000000800000800") +
                           submitted.to_bytes(2, "little") + b"\xa5\x5a\0\0\0\0")
                    memory(0x4000ac00, raw)
                    write(0x4000a0d0, 1)
                    submitted += 1
                    tx = tx_state(0)
                    if tx["pending_frames"]:
                        break
                expect(tx["pending_frames"] == 1 and 0 < tx["pending_bytes"] <= 84,
                       f"did not establish native backpressure: {tx!r}")
                blocked = tx["pending_bytes"]
                report["prefix"] = {"submitted": submitted, **tx}
                memory(0x4000ac00, TX_RAM[:16])
                restored = tx_state(0)
                expect(restored["pending_frames"] == 1 and
                       restored["pending_bytes"] == blocked, "slot restore purged queue")
                stopped(0, 0, "prelaunch")

                phase = "fences"
                deadline = min(overall, time.monotonic() + 10.0)
                report["fences"] = []
                for ledger, target in enumerate(TARGETS):
                    command("x-run-to-virtual-time", {"target-ns": target})
                    while True:
                        execution = command("x-query-virtual-execution")
                        expect(execution["target-ns"] == target, "native target changed")
                        if execution["state"] == "reached":
                            break
                        expect(execution["state"] == "running" and
                               execution["current-ns"] <= target, "invalid execution state")
                    for _ in range(4):
                        stopped(target, ledger)
                    tx = tx_state(target)
                    expect(tx["pending_frames"] == 1 + 2 * ledger and
                           tx["pending_bytes"] == blocked + 168 * ledger,
                           f"unexpected fence backlog: {tx!r}")
                    report["fences"].append({"target_ns": target, "ledger": ledger, **tx})
                expect(read(0xe000e010) & 7 == 0, "SysTick producer is not disabled")
                expect(read(0xe000ed04) & (0x1ff | (1 << 26)) == 0,
                       "SysTick remains active or pending")

                # Transparent observers delegate once, retaining the sole parser
                # and reader. The sink separately checks decoded meaningful data.
                original_records, original_read = can.receive_records, can.read_once

                def read_once():
                    nonlocal empty_read
                    before = len(can.buffer)
                    observed["read_calls"] += 1
                    try:
                        original_read()
                    except BaseException as exc:
                        event("read_error", error=type(exc).__name__)
                        raise
                    added = len(can.buffer) - before
                    expect(0 <= added <= 4096, "canonical read exceeded its byte bound")
                    observed["max_read_bytes"] = max(observed["max_read_bytes"], added)
                    entry = event("read", added_bytes=added,
                                  tail_before=before, tail_after_read=len(can.buffer),
                                  preceding_tx_sequence=last_tx["sequence"],
                                  preceding_pending_frames=last_tx["pending_frames"])
                    if before == 0 and not can.buffer:
                        empty_read = entry

                def records():
                    for raw in original_records():
                        index = observed["wire_count"]
                        expect(raw == expected_wire(index, submitted),
                               f"native wire mismatch at record {index}: {raw.hex()}")
                        if index >= submitted:
                            observed["cpu_timestamps_ns"].append(
                                int.from_bytes(raw[12:20], "little"))
                        wire_hash.update(raw)
                        observed["wire_count"] += 1
                        yield raw
                    expect(len(can.buffer) < 84, "canonical parser retained a complete record")

                def sink(frame):
                    index = observed["sink_count"]
                    if index < submitted:
                        expected = (0x200, 0, 8, 0,
                                    index.to_bytes(2, "little") + b"\xa5\x5a\0\0\0\0")
                    else:
                        cpu = index - submitted
                        expect(0 <= cpu < 6, "extra decoded CPU record")
                        stamp = TIMESTAMPS[cpu]
                        expect((cpu // 2 + 1) * 1000000 <= stamp <=
                               (cpu // 2 + 1) * 1000000 + 17, "CPU time outside old bound")
                        expected = (0x200 if cpu % 2 == 0 else 0x1ff, 0, 8,
                                    stamp, PAYLOADS[cpu % 2])
                    expect(frame == expected, f"decoded record {index} mismatch: {frame!r}")
                    observed["sink_count"] += 1

                can.read_once, can.receive_records = read_once, records
                phase = "drain"
                deadline = min(overall, time.monotonic() + 2.0)
                drain_paused_can(SimpleNamespace(command=command), can, sink,
                    target_ns=3000200, controller="dm-mc02.fdcan1", chardev="can1",
                    tx_dropped=0, deadline=deadline, check=check)
                expect(observed["wire_count"] == observed["sink_count"] == submitted + 6,
                       f"finite record count mismatch: {observed!r}")
                expect(not can.buffer, "CAN physical framing tail remains")
                expect(empty_read is not None and
                       empty_read["preceding_pending_frames"] == 0 and
                       empty_read["preceding_tx_sequence"] < empty_read["sequence"] <
                       last_tx["sequence"], "missing causal zero/read/final observation")
                report["closure"] = {**observed, "wire_sha256": wire_hash.hexdigest(),
                                     "fresh_empty_read": empty_read,
                                     "final_tx_sequence": last_tx["sequence"]}
                for _ in range(4):
                    final = stopped(3000200, 3)
                final.update(systick=read(0xe000e010), icsr=read(0xe000ed04),
                             tx=tx_state(3000200), connected=qom("/chardevs/can1", "connected"))
                expect(final["systick"] & 7 == 0, "SysTick restarted")
                expect(final["icsr"] & (0x1ff | (1 << 26)) == 0, "IRQ state changed")
                expect(final["tx"]["pending_bytes"] == 0 and final["connected"] is True,
                       "native backlog or connection changed")
                report["final_native"] = final

                # Native reaccept can report connected again. The completed
                # original stream must still expose EOF to the same operation.
                phase = "original_peer_eof"
                deadline = min(overall, time.monotonic() + 2.0)
                can_socket.shutdown(socket.SHUT_WR)
                connected(False)
                replacement = connect_unix(str(owner.sockets["can1"]),
                                           min(2.0, check()))
                peers.append(("replacement CAN", replacement))
                connected(True)
                before = dict(observed)
                try:
                    drain_paused_can(SimpleNamespace(command=command), can, sink,
                        target_ns=3000200, controller="dm-mc02.fdcan1", chardev="can1",
                        tx_dropped=0, deadline=deadline, check=check)
                except ConnectionError as exc:
                    expect(str(exc) == "QEMU FDCAN socket closed", f"wrong EOF error: {exc}")
                    expect(observed["wire_count"] == before["wire_count"] and
                           observed["sink_count"] == before["sink_count"],
                           "EOF attempt published another record")
                    report["original_peer_eof"] = {"error": str(exc),
                        "replacement_connected": True, "original_stream_retained": True}
                else:
                    raise AssertionError("original EOF was accepted as output completion")
                stopped(3000200, 3)
                expect(tx_state(3000200)["pending_bytes"] == 0, "EOF gate created backlog")
                check()
            except BaseException as exc:
                primary = exc
                raise
            finally:
                for name, peer in reversed(peers):
                    try:
                        peer.close()
                    except BaseException as exc:
                        peer_cleanup_errors.append(f"{name}: {exc}")
                        if primary is None:
                            primary = exc
                        else:
                            primary.add_note(f"{name} cleanup failed: {exc}")
                if sys.exc_info()[1] is None and primary is not None:
                    raise primary
    except BaseException as exc:
        primary = exc
    finally:
        report["stage"] = phase
        report["trace_tail"] = list(trace)
        report["trace_events"] = sequence
        report["observed"] = observed
        cleanup_errors = list(peer_cleanup_errors)
        for label, action in (
            ("native process", lambda: expect(owner is None or
                starttime(owner.process.pid) != report["process"]["starttime"],
                "native PID/starttime identity remains")),
            ("socket directory", lambda: expect(owner is None or
                not owner.stderr_path.parent.exists(), "private socket directory remains")),
            ("guest image directory", lambda: image_dir.cleanup() if image_dir else None),
            ("guest image removal", lambda: expect(image_dir is None or
                not Path(image_dir.name).exists(), "guest image directory remains")),
        ):
            try:
                action()
            except BaseException as exc:
                cleanup_errors.append(f"{label}: {exc}")
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"{label} cleanup verification failed: {exc}")
        report["cleanup_errors"] = cleanup_errors
        report["cleanup_verified"] = not cleanup_errors
    if primary is not None:
        raise primary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "failed", "guest_sha256": GUEST_SHA, "claims":
        "Finite quiescent paused output only; no physical CAN, CPU/world or timer barrier."}
    primary = None

    def interrupted(signum, _frame):
        raise InterruptedError(f"native paused CAN interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    # Reserve the report before starting resources; never overwrite prior proof.
    with args.report.open("x", encoding="utf-8") as output:
        try:
            binary = resolve_qemu(args.qemu)
            report["qemu_argument"] = {"path": str(binary), "sha256": sha(binary)}
            sources = [Path(__file__).resolve(),
                ROOT / "qemu/upstream/tests/qtest/dm-mc02-cpu-test.c",
                ROOT / "qemu/upstream/pc-bios/efi-virtio.rom"]
            for name in ("dm_mc02_firmware_rtf", "dm_mc02_qmp", "dm_mc02_test_harness",
                         "qemu.machine.qtest", "sim.boards", "sim.boards.qemu",
                         "sim.boards.launch", "sim.transports.can_stream",
                         "sim.transports.stream", "sim.transports.can_codec",
                         "sim.transports.unix"):
                source = Path(sys.modules[name].__file__).resolve()
                expect(source.is_relative_to(ROOT), f"module outside checkout: {name}")
                sources.append(source)
            report["sources"] = {str(path): sha(path) for path in sources}
            run(binary, report)
            report["status"] = "passed"
        except BaseException as exc:
            primary = exc
            report["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "notes": getattr(exc, "__notes__", [])}
        try:
            json.dump(report, output, indent=2)
            output.write("\n")
        except BaseException as exc:
            if primary is None:
                primary = exc
            else:
                primary.add_note(f"report publication failed: {exc}")
    if primary is not None:
        raise primary
    print(f"RESULT: paused CAN native publication and original-peer EOF passed; {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
