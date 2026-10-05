"""Fixed wire/report and lifecycle gates; real UART firmware is tested separately."""

from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_referee_test.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_referee_consumer_test", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

# Official 2026 V1.1.0 packed payloads with fixed sequences 0..7. CRC literals
# were calculated directly with pinned crccheck, without the project encoders.
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
        assert endpoint.flush() is False and [row["bytes"] for row in budget.writes] == [82, 19]
        budget.budget = None
        assert endpoint.flush() is True and not stream.tx_pending
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


def test_missing_input_preserves_missing_coverage_and_refuses_overwrite(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure admission must not acquire a process")
    monkeypatch.setattr(module, "running_qemu", forbidden)
    output = tmp_path / "results"
    assert module.run(tmp_path / "missing.elf", output, tmp_path) == 1
    report_path = output / "report.json"
    report = json.loads(report_path.read_text())
    assert report["result"] == "failed" and report["cases"] == []
    assert report["missing"] == sorted(REQUIRED)
    assert report["missing_skips"] == ["plant.physics"]
    assert any("RefereeTest ELF" in error for error in report["errors"])
    original = report_path.read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        module.run(tmp_path / "missing.elf", output, tmp_path)
    assert report_path.read_bytes() == original


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
