#!/usr/bin/env python3
"""Finite native consumer of paused CAN output and input completion receipts.

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
from sim.boards import (drain_paused_can, observe_initial_paused_can,
                        wait_paused_can_input)
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
# target, completed-pair ledger, cumulative CPU frames already admitted.
TARGETS = ((1000000, 0, 0), (1000004, 0, 1), (1000006, 0, 2),
           (2000000, 1, 2), (3000000, 2, 4), (3000200, 3, 6))
PAYLOADS = (bytes.fromhex("1122334455667788"),
            bytes.fromhex("8877665544332211"))
# q8c1 fixed84 ID0x321/DLC8, metadata timestamp1ms and literal payload.
INPUT_WIRE = bytes.fromhex(
    "21030000000000000800000040420f00000000001122334455667788") + bytes(56)


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
    qmp_groups = {"calls": 0, "completed": 0, "sizes": {}, "phases": {}}
    wire_hash = hashlib.sha256()
    last_tx = None
    last_rx = None
    empty_read = None
    first_input_rx = None
    first_input_flush = None
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

    def observe_result(name, arguments, result):
        nonlocal last_tx, last_rx, first_input_rx
        if name == "qom-get" and arguments.get("property") == "can-execution-state":
            for prop in ("fdcan-tx-state", "fdcan-rx-state"):
                observe_result("qom-get", {"property": prop}, result[prop])
            return
        if name == "qom-get" and arguments.get("property") == "fdcan-tx-state":
            tx = result["controllers"]["dm-mc02.fdcan1"]
            last_tx = event("tx", timestamp_ns=result["timestamp_ns"], **tx)
        if name == "qom-get" and arguments.get("property") == "fdcan-rx-state":
            rx = result["controllers"]["dm-mc02.fdcan1"]
            entry = event("rx", timestamp_ns=result["timestamp_ns"], **rx)
            last_rx = entry
            if phase == "input_suffix_wait" and first_input_rx is None:
                expect(rx == {"completed_frames": 0, "partial_bytes": 83,
                              "rx_dropped": 0}, "helper did not first observe real RX83")
                first_input_rx = entry

    def command(name, arguments=None):
        check()
        result = deadline_qmp_call(
            qmp, deadline, f"paused CAN {phase}",
            lambda: qmp.command(name, arguments))
        check()
        observe_result(name, arguments, result)
        return result

    def command_many(commands):
        check()
        qmp_groups["calls"] += 1
        values = deadline_qmp_call(
            qmp, deadline, f"paused CAN {phase}",
            lambda: qmp.command_many(commands))
        check()
        qmp_groups["completed"] += 1
        for counts, key in ((qmp_groups["sizes"], str(len(commands))),
                            (qmp_groups["phases"], phase)):
            counts[key] = counts.get(key, 0) + 1
        # Record actual values after the whole group returns. These event
        # sequence numbers do not represent individual reply arrival times.
        for (name, arguments), value in zip(commands, values, strict=True):
            observe_result(name, arguments, value)
        return values

    helper_qmp = SimpleNamespace(command=command, command_many=command_many)

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

    def rx_state(stamp):
        state = qom("/machine", "fdcan-rx-state")
        expect(state["schema"] == 1 and state["timestamp_ns"] == stamp,
               f"unexpected native RX state: {state!r}")
        return state["controllers"]["dm-mc02.fdcan1"]

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
        sockets = ("qmp", "cosim", "can1", "qtest")
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
                cosim_socket = connect_unix(str(owner.sockets["cosim"]),
                                            min(2.0, check()))
                peers.append(("co-sim", cosim_socket))
                can_socket = connect_unix(str(owner.sockets["can1"]),
                                          min(2.0, check()))
                peers.append(("original CAN", can_socket))
                can = CanStream(can_socket)
                connected(True)
                while qom("/chardevs/cosim", "connected") is not True:
                    check()
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
                # One classic RX slot at byte offset0x40, outside both TX slots.
                write(0x4000a0a0, 0x00010040)
                write(0x4000a0bc, 0)
                write(0x4000a080, 0)  # GFC accepts nonmatching frames in FIFO0.
                memory(0x4000ac40, bytes(16))
                expect(read(0x4000a054) == read(0x4000a05c) == 0,
                       "FDCAN IRQ delivery must remain disabled")
                write(0x4000a018, 0)
                write(0x58024418, 0xa00)
                write(0xe000e014, 7999)
                write(0xe000e018, 0)
                write(0xe000e010, 7)
                expect(tx_state(0)["pending_frames"] == 0, "unexpected initial backlog")
                expect(rx_state(0) == {"completed_frames": 0, "partial_bytes": 0,
                                       "rx_dropped": 0}, "unexpected initial RX baseline")

                phase = "wrong_transport_identity"
                bound = qom("/machine", "can-execution-state")["fdcan-transports"][
                    "dm-mc02.fdcan1"]
                unrelated = qom("/chardevs/cosim", "connected")
                expect(bound == {"path": "/chardevs/can1", "connected": True} and
                       unrelated is True, "native connection identities are not established")
                before_tx, before_rx = tx_state(0), rx_state(0)
                try:
                    observe_initial_paused_can(helper_qmp, can, timestamp_ns=0,
                        controller="dm-mc02.fdcan1", chardev="cosim",
                        deadline=deadline, check=check)
                except RuntimeError as exc:
                    expect("transport identity changed" in str(exc),
                           f"wrong connection identity failed unexpectedly: {exc}")
                    report["wrong_transport_identity"] = {
                        "requested_chardev": "cosim", "actual_transport": bound,
                        "unrelated_connected": unrelated, "error": str(exc)}
                else:
                    raise AssertionError("unrelated connected socket accepted as CAN transport")
                expect(tx_state(0) == before_tx and rx_state(0) == before_rx and
                       not can.buffer and not can.tx_pending,
                       "identity rejection changed CAN state")
                stopped(0, 0, "prelaunch")

                phase = "initial_baseline"
                initial = observe_initial_paused_can(
                    helper_qmp, can, timestamp_ns=0,
                    controller="dm-mc02.fdcan1", chardev="can1",
                    deadline=deadline, check=check)
                expect(initial == {
                    "tx": {"pending_frames": 0, "pending_bytes": 0,
                           "tx_dropped": 0, "tx_short_writes": 0},
                    "rx": {"completed_frames": 0, "partial_bytes": 0,
                           "rx_dropped": 0},
                }, f"unexpected initial helper baseline: {initial!r}")
                report["initial_baseline"] = initial
                stopped(0, 0, "prelaunch")

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

                # Transparent observers delegate once, retaining the sole parser
                # and reader. The sink separately checks decoded meaningful data.
                original_records, original_read = can.receive_records, can.read_once
                original_flush = can.flush

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
                                  preceding_tx=last_tx,
                                  preceding_tx_sequence=last_tx["sequence"],
                                  preceding_pending_frames=last_tx["pending_frames"])
                    if before == 0 and not can.buffer:
                        empty_read = entry

                def flush():
                    nonlocal first_input_flush
                    entry = event("flush", queued_bytes=sum(map(len, can.tx_queue)))
                    if phase == "input_suffix_wait" and first_input_flush is None:
                        first_input_flush = entry
                        expect(first_input_rx is not None and
                               first_input_rx["sequence"] < entry["sequence"] and
                               entry["queued_bytes"] == 1,
                               "suffix flush preceded native RX83 observation")
                    result = original_flush()
                    event("flushed", queued_bytes=sum(map(len, can.tx_queue)))
                    return result

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

                can.read_once, can.receive_records, can.flush = read_once, records, flush
                report["fences"], report["closures"] = [], []
                for target, ledger, cpu_frames in TARGETS:
                    phase = "fences"
                    deadline = min(overall, time.monotonic() + 2.0)
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
                    icsr = read(0xe000ed04)
                    if target == 1000000:
                        expect(icsr & (1 << 26) or icsr & 0x1ff == 15,
                               "equal-time SysTick is neither pending nor active")
                    elif target in (1000004, 1000006):
                        expect(icsr & 0x1ff == 15, "mid-handler fence is not exception15")
                    tx = tx_state(target)
                    if target == 1000000:
                        expect(tx["pending_frames"] == 1 and tx["pending_bytes"] == blocked,
                               "first fence changed the preblocked suffix")
                    report["fences"].append({"target_ns": target, "ledger": ledger,
                                             "icsr": icsr, "cpu_frames": cpu_frames, **tx})
                    phase = "drain"
                    empty_read = None
                    drain_paused_can(helper_qmp, can, sink,
                        target_ns=target, controller="dm-mc02.fdcan1", chardev="can1",
                        tx_dropped=0, deadline=deadline, check=check)
                    expect(observed["wire_count"] == observed["sink_count"] ==
                           submitted + cpu_frames, f"fence record count mismatch: {observed!r}")
                    expect(not can.buffer, "CAN physical framing tail remains")
                    expect(empty_read is not None and
                           empty_read["preceding_pending_frames"] == 0 and
                           empty_read["preceding_tx_sequence"] < empty_read["sequence"] <
                           last_tx["sequence"], "missing causal zero/read/final observation")
                    stopped(target, ledger)
                    expect(read(0xe000ed04) == icsr, "IRQ state changed while draining")
                    report["closures"].append({"target_ns": target,
                        "wire_count": observed["wire_count"],
                        "sink_count": observed["sink_count"],
                        "fresh_empty_read": empty_read,
                        "final_tx": last_tx,
                        "final_tx_sequence": last_tx["sequence"]})

                    if target == 1000000:
                        phase = "input_partial"
                        # W1C the completed prefill TX flag before the RX baseline.
                        write(0x4000a050, 0xffffffff)
                        expect(rx_state(target) == {"completed_frames": 0,
                               "partial_bytes": 0, "rx_dropped": 0}, "input baseline changed")
                        check()
                        expect(can_socket.send(INPUT_WIRE[:83]) == 83,
                               "native socket did not accept the exact83-byte prefix")
                        check()
                        while True:
                            rx = rx_state(target)
                            expect(rx["completed_frames"] == rx["rx_dropped"] == 0 and
                                   0 <= rx["partial_bytes"] <= 83, "prefix completed early")
                            if rx["partial_bytes"] == 83:
                                break
                        stopped(target, ledger)
                        expect(read(0x4000a0a4) & 0x7f == 0 and read(0x4000a050) == 0,
                               "partial wire produced FIFO data or IRQ")
                        expect(all(read(0x4000ac40 + offset) == 0
                                   for offset in range(0, 16, 4)), "partial wire changed RAM")
                        report["input_partial"] = dict(rx)

                        # Stage the physical one-byte suffix in the existing queue.
                        expect(not can.tx_pending and not can.buffer, "stream is not drained")
                        can.tx_queue.append(memoryview(INPUT_WIRE[83:]))
                        phase = "input_suffix_wait"
                        receipt = wait_paused_can_input(helper_qmp, can,
                            target_ns=target, controller="dm-mc02.fdcan1", chardev="can1",
                            completed_frames=1, deadline=deadline, check=check)
                        expect(receipt == {"completed_frames": 1, "partial_bytes": 0,
                                          "rx_dropped": 0}, "wrong completed input receipt")
                        expect(first_input_rx is not None and first_input_flush is not None,
                               "missing native partial-before-flush evidence")
                        returned = event("input_return", **receipt)
                        expect(last_rx["completed_frames"] == 1 and
                               first_input_flush["sequence"] < last_rx["sequence"] <
                               returned["sequence"], "missing native completion before return")
                        stopped(target, ledger)
                        expect(read(0x4000a0a4) & 0x7f == 1 and read(0x4000a050) == 1,
                               "completed input did not publish FIFO and RF0N")
                        stored = [read(0x4000ac40 + offset) for offset in range(0, 16, 4)]
                        expect(stored[0] == 0x0c840000 and
                               stored[1] & 0x800f0000 == 0x80080000 and
                               stored[2:] == [0x44332211, 0x88776655], "wrong RX RAM literals")
                        expect(not can.tx_pending and not can.buffer, "input retained a suffix")
                        report["input_completion"] = {"receipt": receipt, "ram_words": stored,
                            "first_native_partial": first_input_rx,
                            "first_suffix_flush": first_input_flush,
                            "final_native_receipt": last_rx, "returned": returned}

                        phase = "input_fifo_full"
                        check()
                        can.send(0x321, 0, 8, 1000000, PAYLOADS[0])
                        check()
                        rejected = wait_paused_can_input(helper_qmp, can,
                            target_ns=target, controller="dm-mc02.fdcan1", chardev="can1",
                            completed_frames=2, deadline=deadline, check=check)
                        expect(rejected == {"completed_frames": 2, "partial_bytes": 0,
                                           "rx_dropped": 1}, "rejected input was not processed")
                        stopped(target, ledger)
                        # RM0468 IR.RF0F is bit2; the existing RF0N remains set.
                        expect(read(0x4000a0a4) & 0x7f == 1 and read(0x4000a050) == 5,
                               "FIFO-full rejection has wrong FIFO or RF0F state")
                        expect([read(0x4000ac40 + offset) for offset in range(0, 16, 4)] == stored,
                               "rejected input changed the retained frame")
                        report["input_rejection"] = rejected

                expect(observed["cpu_timestamps_ns"] == list(TIMESTAMPS),
                       "original six CPU timestamps changed")
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
                    drain_paused_can(helper_qmp, can, sink,
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
                phase = "original_input_peer_eof"
                expect(not can.tx_pending and not can.buffer, "EOF fixture has stale tails")
                queued = memoryview(INPUT_WIRE)
                can.tx_queue.append(queued)
                try:
                    wait_paused_can_input(helper_qmp, can,
                        target_ns=3000200, controller="dm-mc02.fdcan1", chardev="can1",
                        completed_frames=3, deadline=deadline, check=check)
                except ConnectionError as exc:
                    expect(len(can.tx_queue) == 1 and can.tx_queue[0] is queued and
                           bytes(can.tx_queue[0]) == INPUT_WIRE,
                           "input EOF consumed or changed the queued original suffix")
                    expect(observed["wire_count"] == before["wire_count"] and
                           observed["sink_count"] == before["sink_count"],
                           "input EOF published an output record")
                    expect(rx_state(3000200) == {"completed_frames": 2,
                           "partial_bytes": 0, "rx_dropped": 1},
                           "replacement peer changed the native input receipt")
                    report["original_input_peer_eof"] = {"error": str(exc),
                        "replacement_connected": True, "original_stream_retained": True,
                        "queued_suffix_bytes": len(queued), "queued_suffix_retained": True}
                else:
                    raise AssertionError("original EOF was accepted as input completion")
                expect(qmp_groups["calls"] == qmp_groups["completed"],
                       "a native QMP observation group did not complete")
                expect(qmp_groups["phases"].get("initial_baseline") == 2,
                       "initial helper did not use two fresh observation groups")
                expect(qmp_groups["phases"].get("wrong_transport_identity") == 1,
                       "identity rejection did not use one fresh observation group")
                expect(set(qmp_groups["sizes"]) == {"1"} and
                       all(qmp_groups["phases"].get(name, 0) > 0 for name in
                           ("drain", "input_suffix_wait", "input_fifo_full",
                            "original_peer_eof", "original_input_peer_eof")),
                       "native drain/receipt/EOF helper missed public batch queries")
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
        report["qmp_observation_groups"] = qmp_groups
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
        "Restricted paused CAN publication and processed input receipts; "
        "no firmware-consumption, physical CAN, CPU/world or timer barrier."}
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
                         "qemu.qmp.legacy", "qemu.qmp.qmp_client",
                         "qemu.qmp.message", "qemu.qmp.protocol",
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
    print(f"RESULT: paused CAN native publication/input receipts and original-peer EOF passed; "
          f"{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
