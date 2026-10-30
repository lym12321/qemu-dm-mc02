"""Fixed wire/report and lifecycle gates; real UART firmware is tested separately."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_referee_test.py"

SPEC = importlib.util.spec_from_file_location("dm_mc02_referee_consumer_test", PATH)

module = importlib.util.module_from_spec(SPEC)

sys.modules[SPEC.name] = module

SPEC.loader.exec_module(module)

REAL_CONSUME = module.consume

from tools import dm_mc02_scene_smoke as arena_module

WIRES = tuple(bytes.fromhex(value) for value in (
    "a50b000002010021010008070605040302017d07"
    "a50d00018d010203026400c80003006400500005ef8a"
    "a50e00028b0202000000000000000028000a001400c1da"
    "a5080003040402050900070b0d00feec0b",
    "a50b00046301003103000807060504030201f59e"
    "a50d0005ec010203024600c800030064007800060a27"
    "a50e0006ea0202000000000000000028000c001900c5f8"
    "a508000765040208110013171d0080d577",
))

REQUIRED = {
    "control.rx", "reset.poweron", "bsp.init", "task.stack", "referee.executor",
    "referee.initial.fresh", "referee.initial.game", "referee.initial.robot",
    "referee.initial.power", "referee.initial.buff", "referee.partial.stable",
    "referee.complete.fresh", "referee.complete.game", "referee.complete.robot",
    "referee.complete.power", "referee.complete.buff", "referee.uart",
}


def test_exact_coverage_and_only_physics_skip():
    assert module.REQUIRED == REQUIRED
    assert module.STAGES == (("referee.initial", 82, 0),
                             ("referee.partial", 19, 82),
                             ("referee.complete", 82, 1))
    module.check_case(dict(status="SKIP", name="plant.physics", actual=0, expected=0))
    for status, name in (("SKIP", "referee.uart"), ("PASS", "plant.physics"),
                         ("FAIL", "control.rx")):
        with pytest.raises(ValueError, match="failed or unexpected"):
            module.check_case(dict(status=status, name=name, actual=0, expected=0))


@pytest.mark.parametrize("name,good,bad,expected", [
    ("reset.poweron", 0x800000, 0x1800000, 0x800000),  # PORRSTF alone, bit 23
    ("task.stack", 96, 63, 64),
    ("referee.initial.fresh", 1, 0, 1),
    ("referee.partial.stable", 1, 0, 1),
    ("referee.complete.game", 0, 1, 0),
    ("referee.executor", 0, 1, 0),
])
def test_firmware_case_requires_literal_expected_fields(name, good, bad, expected):
    row = dict(status="PASS", name=name, actual=good, expected=expected)
    module.check_case(row)
    with pytest.raises(ValueError, match="fields disagree"):
        module.check_case(dict(row, actual=bad))
    with pytest.raises(ValueError, match="fields disagree"):
        module.check_case(dict(row, expected=expected + 1))


@pytest.mark.parametrize("name,field,value", [
    ("referee.initial.fresh", "actual", True),
    ("control.rx", "expected", -1),
    ("task.stack", "actual", 0x100000000),
])
def test_report_fields_are_exact_uint32(name, field, value):
    actual, expected = ((64, 64) if name == "task.stack" else
                        (1, 1) if name.endswith(".fresh") else (0, 0))
    row = dict(status="PASS", name=name, actual=actual, expected=expected)
    row[field] = value
    with pytest.raises(ValueError, match="uint32"):
        module.check_case(row)


def test_real_socket_retains_original_second_batch_suffix():
    sender, peer = socket.socketpair()
    sender.setblocking(False)
    peer.settimeout(1)
    engine = module.create_engine()
    budget = module.SendBudget(sender)
    stream = module.StreamTransport(budget, max_pending=1)
    endpoint = module.RefereeUartEndpoint(stream)
    try:
        assert budget.fileno() == sender.fileno() and budget.gettimeout() == 0
        assert endpoint.transport is stream and not endpoint.closed and not engine.closed
        origin = 12_345_678
        engine.reset(module.Session(1, origin))
        initial = endpoint.send_state(engine.get_state())
        assert initial == WIRES[0]
        assert peer.recv(4096) == WIRES[0]
        assert endpoint.next_sequence == 4 and not stream.tx_pending
        result = engine.step(module.StepContext(1, 1, origin + 1_000_000_000,
                                                1_000_000_000))
        assert result.status == "ok" and result.reached_time_ns == origin + 1_000_000_000
        budget.budget = 19
        second = endpoint.send_state(engine.get_state())
        assert second == WIRES[1]
        assert peer.recv(4096) == WIRES[1][:19]
        assert len(stream.tx_queue) == 1 and stream.tx_queue[0].obj is second
        assert bytes(stream.tx_queue[0]) == WIRES[1][19:]
        assert len(stream.tx_queue[0]) == 63 and endpoint.next_sequence == 8
        assert endpoint.transport.flush() is False and [row["bytes"] for row in budget.writes] == [82, 19]
        budget.budget = None
        assert endpoint.transport.flush() is True and not stream.tx_pending
        assert peer.recv(4096) == WIRES[1][19:]
        assert [row["bytes"] for row in budget.writes] == [82, 19, 63]
        assert bytes(budget.sent) == b"".join(WIRES) and endpoint.next_sequence == 8
        for batch, wire in enumerate(WIRES):
            assert [row["sequence"] for row in module.check_wire(wire, batch)] == list(
                range(batch * 4, batch * 4 + 4))
    finally:
        endpoint.close()
        engine.close()
        sender.close()
        peer.close()


def test_wire_receipt_rejects_wrong_payload_or_partial_snapshot():
    changed = bytearray(WIRES[1])
    changed[7] ^= 0x10
    with pytest.raises(ValueError, match="literal wire contract"):
        module.check_wire(bytes(changed), 1)
    with pytest.raises(ValueError, match="invalid fixed"):
        module.check_wire(WIRES[1][:-1], 1)


def test_checkout_observation_records_actual_unborn_repository(tmp_path, monkeypatch):
    project, firmware = tmp_path / "project", tmp_path / "firmware"
    for repo in (project, firmware):
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True,
                       capture_output=True, timeout=2)
        (repo / "input.cc").write_text("fixed uncommitted source\n")
    (project / "qemu/upstream").mkdir(parents=True)
    for name in ("robomaster", "msg", "utils"):
        (firmware / "components" / name).mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", project)
    receipt = module.checkout_snapshot(firmware)
    assert len(receipt) == 6
    for row in receipt.values():
        assert row["head"] is None
        assert row["head_unavailable"] == "repository has no commit; input files are hashed"
        assert row["status"].startswith("?? ") and row["status"].endswith("input.cc")


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    """Isolate only launch/observation scope, using a completed consumer receipt."""
    elf = tmp_path / "fixture.elf"
    elf.write_bytes(b"fixed input")
    stderr = tmp_path / "stderr"
    stderr.write_bytes(b"")
    process = SimpleNamespace(pid=123456, returncode=None)
    owner = SimpleNamespace(process=process, stderr_path=stderr)
    events = []
    monkeypatch.setattr(module, "source_snapshot", lambda *args: {
        str(elf): hashlib.sha256(elf.read_bytes()).hexdigest()})
    monkeypatch.setattr(module, "checkout_snapshot", lambda firmware: {})
    monkeypatch.setattr(module, "_identity", lambda pid: None)

    def completed(owner, report, engine, resources):
        # This already-validated receipt isolates post-consumer lifecycle; it
        # does not implement a firmware/parser/QMP/engine state machine.
        report["processes"] = {"qemu": dict(pid=process.pid, starttime=1)}
        for name in sorted(REQUIRED):
            expected = (64 if name == "task.stack" else 0x800000 if name == "reset.poweron"
                        else 1 if name.endswith((".fresh", ".stable")) else 0)
            report["cases"].append(dict(status="PASS", name=name,
                                         actual=expected, expected=expected))
        report["cases"].append(dict(status="SKIP", name="plant.physics", actual=0, expected=0))
        events.append("consumer completed")
    monkeypatch.setattr(module, "consume", completed)
    write_json = module.write_json

    def observed_write(path, value):
        if value["result"] == "passed":
            events.append("published passed")
        write_json(path, value)
    monkeypatch.setattr(module, "write_json", observed_write)
    return elf, owner, events


def test_process_acquisition_failure_cannot_publish_passed(lifecycle, tmp_path, monkeypatch):
    elf, owner, events = lifecycle
    @contextmanager
    def rejected(*args, **kwargs):
        raise OSError("process acquisition failed")
        yield owner
    monkeypatch.setattr(module, "running_qemu", rejected)
    output = tmp_path / "acquisition"
    assert module.run(elf, output, tmp_path, qemu=sys.executable) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["result"] == "failed" and "process acquisition failed" in report["errors"]
    assert report["missing"] == sorted(REQUIRED) and "published passed" not in events


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_success_is_published_only_after_successful_harness_cleanup(
        lifecycle, tmp_path, monkeypatch, cleanup_failure):
    elf, owner, events = lifecycle
    @contextmanager
    def harness(*args, **kwargs):
        try:
            yield owner
        finally:
            owner.process.returncode = 0
            events.append("harness cleanup")
            if cleanup_failure:
                raise OSError("harness cleanup failed")
    monkeypatch.setattr(module, "running_qemu", harness)
    output = tmp_path / "cleanup"
    assert module.run(elf, output, tmp_path, qemu=sys.executable) == int(cleanup_failure)
    report = json.loads((output / "report.json").read_text())
    assert report["cleanup_verified"] is True and report["missing"] == []
    if cleanup_failure:
        assert report["result"] == "failed" and "harness cleanup failed" in report["errors"]
        assert events == ["consumer completed", "harness cleanup"]
    else:
        assert report["result"] == "passed"
        assert events == ["consumer completed", "harness cleanup", "published passed"]


ARENA_WIRES = {key: bytes.fromhex(value) for key, value in {
    ("east", 0):
        "a50b00000201000100000807060504030201d0c9"
        "a50d00018d010203026400c800000064005000058122"
        "a50e00028b0202000000000000000028000a001400c1da"
        "a5080003040402050900070b0d00feec0b",
    ("east", 4):
        "a50b000463010001000008070605040302015379"
        "a50d0005ec010203026400c80000006400500005ae31"
        "a50e0006ea0202000000000000000028000a0014002703"
        "a5080007650402050900070b0d00fe8474",
    ("east", 252):
        "a50b00fcd5010001000008070605040302010098"
        "a50d00fd5a010203026400c80000006400500005b63e"
        "a50e00fe5c0202000000000000000028000a001400e19f"
        "a50800ffd30402050900070b0d00fe034d",
    ("west", 0):
        "a50b000002010002000008070605040302016337"
        "a50d00018d010267024600c800000064007800060e5c"
        "a50e00028b0202000000000000000029000c0019000825"
        "a508000304040208110013171d0080bd08",
    ("west", 4):
        "a50b00046301000200000807060504030201e087"
        "a50d0005ec010267024600c80000006400780006214f"
        "a50e0006ea0202000000000000000029000c001900eefc"
        "a508000765040208110013171d0080d577",
    ("west", 252):
        "a50b00fcd501000200000807060504030201b366"
        "a50d00fd5a010267024600c800000064007800063940"
        "a50e00fe5c0202000000000000000029000c0019002860"
        "a50800ffd3040208110013171d0080524e",
}.items()}

SCENARIO_WIRES = {sequence: bytes.fromhex(value) for sequence, value in {
    0: "a50b00000201000100000807060504030201d0c9"
       "a50d00018d010203024600c80000006400500005296b"
       "a50e00028b0202000000000000000028000f001b005e37"
       "a508000304040208110013171d0080bd08",
    4: "a50b000463010001000008070605040302015379"
       "a50d0005ec010203024600c800000064005000050678"
       "a50e0006ea0202000000000000000028000f001b00b8ee"
       "a508000765040208110013171d0080d577",
}.items()}

ARENA_COMMON = ("control.rx", "reset.poweron", "bsp.init", "task.stack",
                "referee.executor", "referee.uart")

ARENA_GROUPS = ("fresh", "game", "robot", "power", "buff", "id",
                "ts.game", "ts.robot", "ts.power", "ts.buff")

ARENA_REQUIRED = set(ARENA_COMMON) | {
    f"referee.arena.{sample}.{group}" for sample in (1, 2, 3) for group in ARENA_GROUPS}


def arena_rows(name):
    """A fixed valid firmware receipt, not a guest/parser state machine."""
    rows = [dict(status="PASS", name=label, actual=value, expected=value)
            for label, value in zip(ARENA_COMMON, (0, 0x800000, 0, 64, 0, 0))]
    for sample in (1, 2, 3):
        for index, group in enumerate(ARENA_GROUPS):
            value = {"fresh": 1, "id": {"east": 3, "west": 103}[name]}.get(group, 0)
            actual, expected = ((100 * sample + index, 0 if sample == 1 else 100 * (sample - 1) + index)
                                if group.startswith("ts.") else (value, value))
            rows.append(dict(status="PASS", name=f"referee.arena.{sample}.{group}",
                             actual=actual, expected=expected))
    return [*rows, dict(status="SKIP", name="plant.physics", actual=0, expected=0)]


def arena_summaries():
    return [{"kind": kind, "values": [36, 0, 1]} for kind in ("PHASE", "DONE")]


@pytest.mark.parametrize("logical_ns", [None, True, -1, 200_000_000.0])
def test_scenario_wire_requires_plain_nonnegative_logical_time(logical_ns):
    with pytest.raises((TypeError, ValueError), match="logical"):
        arena_module.check_referee_wire(SCENARIO_WIRES[0], "east", 0,
                                       scenario=True, logical_ns=logical_ns)


@pytest.mark.parametrize("corruption", ["crc8", "crc16", "member", "sequence", "partial"])
def test_arena_wire_rejects_wrong_member_sequence_crc_or_incomplete_batch(corruption):
    wire, name, sequence = ARENA_WIRES["east", 0], "east", 0
    if corruption in ("crc8", "crc16"):
        changed = bytearray(wire)
        changed[4 if corruption == "crc8" else -1] ^= 1
        wire = bytes(changed)
    elif corruption == "member":
        name = "west"
    elif corruption == "sequence":
        sequence = 4
    else:
        wire = wire[:-1]
    with pytest.raises(ValueError, match="wire disagrees|complete82-byte"):
        arena_module.check_referee_wire(wire, name, sequence)


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "id", "stale", "zero", "history", "summary", "uint32"])
def test_arena_report_requires_exact_coverage_scoped_id_and_all_timestamp_histories(corruption):
    assert arena_module.REFEREE_REQUIRED == ARENA_REQUIRED and len(ARENA_REQUIRED) == 36
    rows, summaries = arena_rows("east"), arena_summaries()
    arena_module.check_referee_coverage(rows, summaries, "east")
    if corruption == "missing":
        rows.pop(0)
    elif corruption == "duplicate":
        rows.append(rows[0].copy())
    elif corruption == "summary":
        summaries[-1]["values"] = [35, 0, 1]
    else:
        label = "referee.arena.2.id" if corruption == "id" else "referee.arena.2.ts.buff"
        row = next(row for row in rows if row["name"] == label)
        if corruption == "id":
            row["actual"] = row["expected"] = 103
        elif corruption == "stale":
            row["actual"] = row["expected"]
        elif corruption == "zero":
            row["actual"] = 0
        elif corruption == "history":
            row["expected"] += 1
        else:
            row["actual"] = True
    with pytest.raises(ValueError, match="coverage|fields disagree|history|summaries|uint32"):
        arena_module.check_referee_coverage(rows, summaries, "east")
