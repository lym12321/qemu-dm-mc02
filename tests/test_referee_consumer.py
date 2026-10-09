"""Fixed wire/report and lifecycle gates; real UART firmware is tested separately."""

from contextlib import contextmanager
from dataclasses import replace
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
REAL_CONSUME = module.consume

from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from test_sim_shared_world import NativeStub, UartBudget, world_resources
from tools import dm_mc02_arena_smoke as arena_module

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


@pytest.mark.parametrize("shared_world", [False, True])
def test_missing_input_preserves_missing_coverage_and_refuses_overwrite(
        tmp_path, monkeypatch, shared_world):
    def forbidden(*args, **kwargs):
        pytest.fail("pure admission must not acquire a process")
    monkeypatch.setattr(module, "running_qemu", forbidden)
    output = tmp_path / "results"
    assert module.run(tmp_path / "missing.elf", output, tmp_path, shared_world=shared_world) == 1
    report_path = output / "report.json"
    report = json.loads(report_path.read_text())
    assert report["result"] == "failed" and report["cases"] == []
    assert report["missing"] == sorted(REQUIRED)
    assert report["missing_skips"] == ["plant.physics"]
    assert report["shared_world"] is shared_world
    assert any("RefereeTest ELF" in error for error in report["errors"])
    original = report_path.read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        module.run(tmp_path / "missing.elf", output, tmp_path, shared_world=shared_world)
    assert report_path.read_bytes() == original


@pytest.mark.parametrize("shared_world", [False, True])
def test_cli_forwards_defaultoff_shared_world_flag(tmp_path, monkeypatch, shared_world):
    calls = []
    def admitted(elf, output, firmware, **options):
        calls.append((elf, output, firmware, options))
        return 17
    monkeypatch.setattr(module, "run", admitted)
    paths = tuple(str(tmp_path / name) for name in ("firmware.elf", "report", "trobot"))
    arguments = [*paths, "--qemu", "/fixed/qemu"]
    if shared_world:
        arguments.append("--shared-world")
    assert module.main(arguments) == 17
    assert calls == [(*paths, dict(qemu="/fixed/qemu", shared_world=shared_world))]


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


@pytest.fixture
def shared_acquisition(lifecycle, monkeypatch):
    """Borrow the existing launch receipt; acquire a real lower plant owner."""
    elf, owner, events = lifecycle
    acquired = SimpleNamespace(plant=None, native=None, engine=None, factory_calls=[])
    create_engine = module.create_engine

    def engine():
        acquired.engine = create_engine()
        original = acquired.engine.close
        def close():
            events.append("engine close")
            original()
        acquired.engine.close = close
        return acquired.engine

    def plant(path, **options):
        events.append("plant acquire")
        acquired.factory_calls.append((Path(path), options))
        native = acquired.native = NativeStub()
        native.samples["imu"] = native.samples["left"]
        bus = CanBus()
        devices = tuple(M3508Device(bus, motor_id) for motor_id in range(1, 5))
        acquired.plant = M3508JointPlant(native, {
            device: "wheel_" + corner for device, corner in zip(devices, ("fl", "fr", "rl", "rr"))})
        original = acquired.plant.close
        def close():
            events.append("plant close")
            original()
        acquired.plant.close = close
        return acquired.plant

    monkeypatch.setattr(module, "create_engine", engine)
    monkeypatch.setattr(module, "create_four_wheel_plant", plant)
    monkeypatch.setattr(module, "source_origins", lambda: {})
    yield elf, owner, events, acquired
    # Test-only isolation must not leak resources if production rejects before
    # acquiring them. A successfully acquired owner is already terminal.
    if acquired.plant is not None and not acquired.plant.closed:
        acquired.plant.close()
    if acquired.engine is not None and not acquired.engine.closed:
        acquired.engine.close()


def test_shared_native_preconstruction_failure_cleans_engine_before_process(
        shared_acquisition, tmp_path, monkeypatch):
    elf, _, events, acquired = shared_acquisition
    primary = ValueError("native model admission failed")
    def rejected(path, **options):
        events.append("plant rejected")
        raise primary
    def forbidden(*args, **kwargs):
        pytest.fail("failed native admission reached QEMU acquisition")
    monkeypatch.setattr(module, "create_four_wheel_plant", rejected)
    monkeypatch.setattr(module, "running_qemu", forbidden)
    output = tmp_path / "native-rejected"
    assert module.run(elf, output, tmp_path, qemu=sys.executable, shared_world=True) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["result"] == "failed" and str(primary) in report["errors"]
    assert report["missing"] == sorted(REQUIRED) and report["missing_skips"] == ["plant.physics"]
    assert events == ["plant rejected", "engine close"]
    assert acquired.engine.closed and "published passed" not in events


def test_shared_process_acquisition_failure_releases_preconstructed_owners(
        shared_acquisition, tmp_path, monkeypatch):
    elf, owner, events, acquired = shared_acquisition
    @contextmanager
    def rejected(command, *, sockets, **options):
        assert acquired.plant is not None and not acquired.plant.closed
        assert acquired.engine is not None and not acquired.engine.closed
        assert set(sockets) == {"qmp", "uart1", "uart7", "can1", "cosim"}
        events.append("harness rejected")
        raise OSError("shared process acquisition failed")
        yield owner
    monkeypatch.setattr(module, "running_qemu", rejected)
    output = tmp_path / "shared-acquisition"
    assert module.run(elf, output, tmp_path, qemu=sys.executable, shared_world=True) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["result"] == "failed" and "shared process acquisition failed" in report["errors"]
    assert len(acquired.factory_calls) == 1
    assert acquired.factory_calls[0][0] == module.ROOT / "models/four_wheel.xml"
    assert events[0:2] == ["plant acquire", "harness rejected"]
    assert events.count("plant close") == events.count("engine close") == 1
    assert acquired.plant.closed and acquired.engine.closed and acquired.native.closed == 1
    assert "published passed" not in events


def test_shared_acquisition_interrupt_retains_nested_cleanup_notes_and_attempts_all_owners(
        shared_acquisition, tmp_path, monkeypatch):
    elf, owner, events, acquired = shared_acquisition
    primary = KeyboardInterrupt("shared acquisition interrupted")
    cleanup = OSError("plant cleanup failed")
    cleanup.add_note("native storage release failed")
    create_plant, create_engine = module.create_four_wheel_plant, module.create_engine
    def plant(path, **options):
        value = create_plant(path, **options)
        close = value.close
        def failed_close():
            close()
            raise cleanup
        value.close = failed_close
        return value
    def engine():
        value = create_engine()
        close = value.close
        def failed_close():
            close()
            raise primary  # Identical primary must never copy notes onto itself.
        value.close = failed_close
        return value
    @contextmanager
    def rejected(*args, **kwargs):
        events.append("harness interrupted")
        raise primary
        yield owner
    monkeypatch.setattr(module, "create_four_wheel_plant", plant)
    monkeypatch.setattr(module, "create_engine", engine)
    monkeypatch.setattr(module, "running_qemu", rejected)
    output = tmp_path / "shared-interrupted"
    assert module.run(elf, output, tmp_path, qemu=sys.executable, shared_world=True) == 130
    report = json.loads((output / "report.json").read_text())
    assert report["result"] == "failed" and str(primary) in report["errors"]
    notes = "\n".join(report["notes"])
    assert str(cleanup) in notes and "native storage release failed" in notes
    assert len(report["cleanup_errors"]) == 2
    assert events.count("plant close") == events.count("engine close") == 1
    assert acquired.plant.closed and acquired.engine.closed and acquired.native.closed == 1
    assert "published passed" not in events


@pytest.mark.parametrize("owned", [False, True])
def test_shared_consumer_acquisition_cleanup_respects_runtime_transfer(
        shared_acquisition, tmp_path, monkeypatch, owned):
    elf, owner, events, acquired = shared_acquisition
    owner.start_ns = module.time.monotonic_ns()
    owner.process.poll = lambda: owner.process.returncode
    owner.sockets = {name: tmp_path / name for name in ("qmp", "uart1", "uart7", "can1", "cosim")}
    identity = dict(pid=owner.process.pid, starttime=1)
    monkeypatch.setattr(module, "_identity", lambda pid: identity if owner.process.returncode is None else None)
    monkeypatch.setattr(module, "consume", REAL_CONSUME)
    closes, peers = {}, []
    sockets = {}
    class BorrowedSocket:
        def __init__(self, name, sock):
            self.name, self.sock = name, sock
        def __getattr__(self, name):
            return getattr(self.sock, name)
        def close(self):
            closes[self.name] = closes.get(self.name, 0) + 1
            self.sock.close()
    for name in ("uart1", "uart7", "can1", "cosim"):
        sender, peer = socket.socketpair()
        sender.setblocking(False)
        peer.setblocking(False)
        sockets[name] = BorrowedSocket(name, sender)
        peers.append(peer)
    monkeypatch.setattr(module, "connect_unix", lambda path, timeout: sockets[Path(path).name])
    def unused_qmp(*args, **kwargs):
        pytest.fail("injected acquisition failure reached a QMP command")
    qmp = SimpleNamespace(command=unused_qmp,
                         close=lambda: closes.__setitem__("qmp", closes.get("qmp", 0) + 1))
    monkeypatch.setattr(module, "QmpSession", lambda *args, **options: qmp)
    original_endpoint_close = module.RefereeUartEndpoint.close
    def endpoint_close(endpoint):
        events.append("endpoint close")
        original_endpoint_close(endpoint)
    monkeypatch.setattr(module.RefereeUartEndpoint, "close", endpoint_close)
    original_runtime = module.SharedWorldRuntime
    observed = []
    primary = OSError("shared RESET before IO failed" if owned else "shared binding admission failed")
    def construct(*args, **options):
        events.append("runtime admission")
        if not owned:
            raise primary
        runtime = original_runtime(*args, **options)
        observed.append(runtime)
        original_close = runtime.close
        def close():
            events.append("runtime close")
            original_close()
        runtime.close = close
        def start(session, wire_sessions, *, deadline):
            assert session == module.Session(1, 0) and wire_sessions == (1,)
            assert runtime.plant is acquired.plant
            assert runtime.referees[0].engine is acquired.engine
            events.append("runtime start rejected")
            raise primary
        runtime.start = start
        return runtime
    monkeypatch.setattr(module, "SharedWorldRuntime", construct)
    @contextmanager
    def harness(*args, **options):
        events.append("harness entered")
        try:
            yield owner
        finally:
            owner.process.returncode = 0
            events.append("harness cleanup")
    monkeypatch.setattr(module, "running_qemu", harness)
    output = tmp_path / ("owned" if owned else "rejected")
    try:
        assert module.run(elf, output, tmp_path, qemu=sys.executable, shared_world=True) == 1
        report = json.loads((output / "report.json").read_text())
        assert report["result"] == "failed" and str(primary) in report["errors"]
        assert report["cleanup_verified"] is True and report["missing"] == sorted(REQUIRED)
        assert report["shared_world"] is True and "published passed" not in events
        assert events.count("endpoint close") == events.count("engine close") == events.count("plant close") == 1
        assert events.count("runtime close") == int(owned)
        assert acquired.plant.closed and acquired.engine.closed and acquired.native.closed == 1
        assert acquired.native.resets == 0 and acquired.native.steps == []
        assert closes == {name: 1 for name in ("qmp", "uart1", "uart7", "can1", "cosim")}
        assert events.index("plant close") < events.index("harness cleanup")
        if owned:
            assert observed[0].closed and observed[0].referees[0].endpoint.closed
    finally:
        for wrapped in sockets.values():
            if wrapped.fileno() >= 0:
                wrapped.sock.close()
        for peer in peers:
            peer.close()


# Same official packed layouts as above, with stable scoped robot IDs 3/103.
# These full-frame literals were calculated with pinned crccheck directly from
# the payload bytes, independently of the production encoder and host oracle.
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


def scenario_observations():
    """One fixed completed observer receipt, without executing an owner."""
    return {"event_ns": 200_000_000,
        "samples": {name: {str(sample): dict(logical_ns=stamp, poll_sample_qemu_ns=10 + sample)
            for sample, stamp in enumerate(stamps, 1)} for name, stamps in (
                ("east", (50_000_000, 200_000_000, 300_000_000)),
                ("west", (50_000_000, 300_000_000, 400_000_000)))},
        "stage": dict(logical_ns=50_000_000, poll_sample_qemu_ns=19),
        "control": dict(logical_ns=200_000_000, completed_logical_ns=200_000_000,
            send_attempts=1, flush_attempts=1, wire_hex="a5", accepted=True, completed=True),
        "shared_start": dict(session_id=1, origin_ns=0, accepted=True),
        "queue_calls": [dict(member=name, session_id=1, logical_ns=0, exact_batch=True,
            offsets_ns=[200_000_000] * 3 if name == "east" else [], before_cont=True, accepted=True)
            for name in ("east", "west")]}


@pytest.mark.parametrize("corruption", ["missing_sample", "late_baseline", "early_changed", "missing_control",
                                       "duplicate_control", "west_before_flush", "queue_session", "queue_after_cont"])
def test_scenario_observation_gate_requires_scoped_causal_and_original_queue_evidence(corruption):
    evidence = scenario_observations()
    arena_module.check_scenario_observations({"scenario_observation": evidence})
    if corruption == "missing_sample":
        del evidence["samples"]["west"]["3"]
    elif corruption == "late_baseline":
        evidence["samples"]["west"]["1"]["logical_ns"] = 200_000_000
    elif corruption == "early_changed":
        evidence["samples"]["east"]["2"]["logical_ns"] = 199_999_999
    elif corruption == "missing_control":
        evidence["control"] = None
    elif corruption == "duplicate_control":
        evidence["control"]["send_attempts"] = 2
    elif corruption == "west_before_flush":
        evidence["samples"]["west"]["2"]["logical_ns"] = 199_999_999
    elif corruption == "queue_session":
        evidence["queue_calls"][1]["session_id"] = 2
    else:
        evidence["queue_calls"][1]["before_cont"] = False
    with pytest.raises(ValueError, match="Scenario referee"):
        arena_module.check_scenario_observations({"scenario_observation": evidence})


@pytest.fixture
def referee_arena_manifest(tmp_path, request):
    elf = tmp_path / "arena.elf"
    elf.write_bytes(b"fixed Arena firmware input")
    directory = tmp_path / "manifests"
    directory.mkdir()
    if getattr(request, "param", False):
        return arena_module.write_scenario_manifest(directory, (elf, elf))
    return arena_module.write_manifests(directory, (elf, elf), referees=True)


@pytest.mark.parametrize("referee_arena_manifest", [True], indirect=True)
def test_scenario_fixture_admits_fixed_typed_inputs_and_exact_wire_time_boundary(referee_arena_manifest):
    from sim.devices.referee import Buff, Hit, ScriptEvent, Shot
    from sim.scenario import load_manifest

    scenario = referee_arena_manifest
    assert load_manifest(scenario.path) == scenario
    assert scenario.duration_ns == 1_000_000_000
    assert [row.member for row in scenario.referees] == ["east", "west"]
    assert scenario.referees[0].events == (
        ScriptEvent(200_000_000, Hit(30)), ScriptEvent(200_000_000, Shot(5, 7)),
        ScriptEvent(200_000_000, Buff(8, 17, 19, 23, 29, 128)))
    assert scenario.referees[1].events == ()
    assert all(row.events == () and row.cooling_42mm_per_s == 0 for row in scenario.arena.referees)
    assert all(not row.robot.upper_computers and row.robot.ready.text ==
               b"TTEST READY referee.arena.ready 0 0\n" for row in scenario.arena.robots)
    for sequence in (0, 4):
        arena_module.check_referee_wire(ARENA_WIRES["east", sequence], "east", sequence,
                                       scenario=True, logical_ns=199_999_999)
        arena_module.check_referee_wire(SCENARIO_WIRES[sequence], "east", sequence,
                                       scenario=True, logical_ns=200_000_000)
        arena_module.check_referee_wire(ARENA_WIRES["west", sequence], "west", sequence,
                                       scenario=True, logical_ns=200_000_000)
        for wrong, stamp in ((SCENARIO_WIRES[sequence], 199_999_999),
                             (ARENA_WIRES["east", sequence], 200_000_000)):
            with pytest.raises(ValueError, match="wire disagrees"):
                arena_module.check_referee_wire(wrong, "east", sequence, scenario=True, logical_ns=stamp)


@pytest.mark.parametrize("logical_ns", [None, True, -1, 200_000_000.0])
def test_scenario_wire_requires_plain_nonnegative_logical_time(logical_ns):
    with pytest.raises((TypeError, ValueError), match="logical"):
        arena_module.check_referee_wire(SCENARIO_WIRES[0], "east", 0,
                                       scenario=True, logical_ns=logical_ns)


@pytest.mark.parametrize("referee_arena_manifest", [True], indirect=True)
def test_scenario_public_queue_drives_real_shared_engines_and_complete_scoped_wires(
        referee_arena_manifest, world_resources, monkeypatch):
    import time
    from sim.core import Session
    from sim.devices.referee import RefereeEngine, RefereeUartEndpoint
    from sim.robots.shared_world import RefereeBinding, SharedWorldRuntime
    from sim.transports import StreamTransport

    scenario = referee_arena_manifest
    plant, boards, native, bindings, _, _, stack = world_resources
    bindings = tuple(replace(row, name=name) for row, name in zip(bindings, ("east", "west")))
    referees, peers, admissions = [], {}, []
    for record in scenario.arena.referees:
        engine = RefereeEngine(record.initial, durations=record.durations,
                               events=record.events, cooling_42mm_per_s=record.cooling_42mm_per_s)
        sender, peer = socket.socketpair()
        sender.setblocking(False)
        peer.settimeout(1)
        stack.callback(sender.close)
        stack.callback(peer.close)
        endpoint = RefereeUartEndpoint(StreamTransport(sender))
        referees.append(RefereeBinding(record.member, engine, endpoint))
        peers[record.member] = peer
        original = engine.queue_events
        def queue(events, *, session_id, original=original, name=record.member):
            admissions.append((name, events, session_id))
            return original(events, session_id=session_id)
        monkeypatch.setattr(engine, "queue_events", queue)
    runtime = SharedWorldRuntime(plant, bindings, referees=tuple(referees), check_running=boards)
    stack.callback(runtime.close)
    origin = 12_345_678
    runtime.start(Session(7, origin), (71, 72), deadline=time.monotonic() + 1)
    for row in scenario.referees:
        runtime.queue_referee_events(row.member, row.events, session_id=7)
    assert len(admissions) == 2 and all(actual[1] is row.events and actual[2] == 7
                                      for actual, row in zip(admissions, scenario.referees))
    assert native.resets == 1 and native.steps == []
    for dt, logical_ns, sequence in ((199_999_999, 199_999_999, 0), (1, 200_000_000, 4)):
        runtime.step(dt, deadline=time.monotonic() + 1)
        for row in referees:
            wire = runtime.publish_referee(row.name, deadline=time.monotonic() + 1)
            expected = (SCENARIO_WIRES[sequence] if logical_ns == 200_000_000 and row.name == "east"
                        else ARENA_WIRES[row.name, sequence])
            assert wire == peers[row.name].recv(4096) == expected
            arena_module.check_referee_wire(wire, row.name, sequence, scenario=True, logical_ns=logical_ns)
    assert runtime.t_sim_ns == origin + 200_000_000 and native.steps == [199_999_999, 1]
    assert referees[0].engine.get_state().robot.current_hp == 70
    assert (referees[0].engine.get_state().power_heat.heat_17mm,
            referees[0].engine.get_state().power_heat.heat_42mm) == (15, 27)
    assert referees[1].engine.get_state() == scenario.arena.referees[1].initial
    runtime.close()
    assert native.closed == 1 and all(row.engine.closed and row.endpoint.closed for row in referees)


def test_arena_admission_drives_real_engines_core_and_uart_with_scoped_literals(referee_arena_manifest):
    from sim.core import Coordinator, Session, StepResult
    from sim.devices.referee import RefereeEngine, RefereeUartEndpoint
    from sim.transports import StreamTransport

    manifest = referee_arena_manifest
    assert [member.name for member in manifest.robots] == ["east", "west"]
    assert all(not member.robot.upper_computers for member in manifest.robots)
    assert all(member.robot.ready.text == b"TTEST READY referee.arena.ready 0 0\n"
               for member in manifest.robots)
    engines = {row.member: RefereeEngine(row.initial, durations=row.durations,
        events=row.events, cooling_42mm_per_s=row.cooling_42mm_per_s) for row in manifest.referees}
    coordinator = Coordinator(engines)
    sockets, endpoints = [], {}
    try:
        for name in engines:
            sender, peer = socket.socketpair()
            sender.setblocking(False)
            peer.settimeout(1)
            sockets.extend((sender, peer))
            endpoints[name] = (RefereeUartEndpoint(StreamTransport(sender)), peer)
        origin = 12_345_678
        coordinator.reset(Session(7, origin))
        for sequence in (0, 4, 252):
            for name, (endpoint, peer) in endpoints.items():
                endpoint.reset(sequence)
                wire = endpoint.send_state(engines[name].get_state())
                assert wire == ARENA_WIRES[name, sequence] == peer.recv(4096)
                arena_module.check_referee_wire(wire, name, sequence)
                assert endpoint.next_sequence == (sequence + 4) % 256
            assert coordinator.step(100_000_000) == {
                name: StepResult("ok", coordinator.t_sim_ns) for name in engines}
        assert coordinator.step(700_000_000) == {
            name: StepResult("ok", origin + 1_000_000_000) for name in engines}
        assert all(engine.get_state() == record.initial
                   for engine, record in zip(engines.values(), manifest.referees))
    finally:
        coordinator.close()
        for endpoint, _ in endpoints.values():
            endpoint.close()
        for sock in sockets:
            sock.close()


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


def test_arena_uart_observer_consumes_fragmented_ready_cases_and_summaries(referee_arena_manifest):
    from sim.boards.channels import ChannelEvent

    report = {"uart": ["", ""], "ready": {}, "cases": {name: [] for name in ("east", "west")},
              "summaries": {name: [] for name in ("east", "west")}}
    observer = arena_module.RefereeObservation(report, referee_arena_manifest)
    observer.runtime = SimpleNamespace(t_sim_ns=20_000_000)
    watchdogs = []
    observer.watchdog = lambda index, name, stage: watchdogs.append((index, name, stage))
    for index, name in enumerate(("east", "west")):
        rows, summaries = arena_rows(name), arena_summaries()
        data = ("startup diagnostic\nTTEST READY referee.arena.ready 0 0\n" +
            "".join(f"TTEST {row['status']} {row['name']} {row['actual']} {row['expected']}\n" for row in rows) +
            "TTEST PHASE 36 0 1\nTTEST DONE 36 0 1\n").encode("ascii")
        for chunk in (data[:19], data[19:91], data[91:]):
            observer.emit(arena_module.arena.ArenaEvent(name, ChannelEvent("uart1", 123 + index, chunk)))
        assert report["cases"][name] == rows and report["summaries"][name] == summaries
        assert report["ready"][name] == {"poll_sample_qemu_ns": 123 + index, "logical_ns": 20_000_000}
        assert not observer.buffers[name]
    assert watchdogs == [(0, "east", "done"), (1, "west", "done")]
    with pytest.raises(ValueError, match="ready marker"):
        observer.emit(arena_module.arena.ArenaEvent("east", ChannelEvent(
            "uart1", 125, b"TTEST READY referee.arena.ready 0 0\n")))
    with pytest.raises(BufferError, match="UART observation exhausted"):
        observer.emit(arena_module.arena.ArenaEvent("west", ChannelEvent("uart1", 125, b"x" * 12001)))


@pytest.mark.parametrize("stage_first", [False, True], ids=["east_changed_first", "west_stage_first"])
def test_scenario_fragmented_stage_borrows_one_control_and_flushes_original_stream(
        referee_arena_manifest, stage_first):
    from sim.boards.channels import ChannelEvent, LabChannels
    from sim.transports import StreamTransport

    report = {"uart": ["", ""], "ready": {}, "cases": {name: [] for name in ("east", "west")},
              "summaries": {name: [] for name in ("east", "west")},
              "scenario_observation": {"event_ns": 200_000_000, "samples": {"east": {}, "west": {}},
                  "stage": None, "control": None, "shared_start": None, "queue_calls": []}}
    observer = arena_module.RefereeObservation(report, referee_arena_manifest, scenario=True)
    observer.runtime = SimpleNamespace(t_sim_ns=50_000_000)
    watchdogs, channel_calls, sockets, budgets, peers = [], [], [], {}, {}
    observer.watchdog = lambda index, name, stage: watchdogs.append((name, stage))
    def channels(**options):
        channel_calls.append(options)
        return LabChannels(**options)
    observer.original_channels = channels
    def emit(name, data):
        observer.emit(arena_module.arena.ArenaEvent(name, ChannelEvent("uart1", 123, data)))
    def emit_rows(name, sample):
        rows = [row for row in arena_rows(name) if row["name"].startswith(f"referee.arena.{sample}.")]
        emit(name, "".join(f"TTEST {row['status']} {row['name']} {row['actual']} {row['expected']}\n"
                           for row in rows).encode("ascii"))
    def stage():
        emit("west", b"TTEST READY referee.scenario.")
        assert report["scenario_observation"]["stage"] is None
        emit("west", b"observe 0 0\n")
    try:
        for name in ("east", "west"):
            sender, peer = socket.socketpair()
            sender.setblocking(False)
            peer.setblocking(False)
            sockets.extend((sender, peer))
            budgets[name], peers[name] = UartBudget(sender), peer
            stream = StreamTransport(budgets[name], max_pending=1)
            actual = observer.channels(uarts={"uart1": stream})
            assert actual is not None and observer.controls[name] is stream
            rows = [row for row in arena_rows(name) if row["name"] in ARENA_COMMON]
            emit(name, b"TTEST READY referee.arena.ready 0 0\n" +
                 "".join(f"TTEST {row['status']} {row['name']} {row['actual']} {row['expected']}\n"
                         for row in rows).encode("ascii"))
            emit_rows(name, 1)
        assert len(channel_calls) == 2
        budgets["west"].budget = 0
        if stage_first:
            stage()
        observer.runtime.t_sim_ns = 200_000_000
        emit_rows("east", 2)
        if not stage_first:
            assert report["scenario_observation"]["control"] is None
            stage()
        control = report["scenario_observation"]["control"]
        assert control["accepted"] and not control["completed"] and control["send_attempts"] == 1
        assert control["borrowed_stream_identity"] == id(observer.controls["west"])
        assert control["flush_attempts"] == budgets["west"].sends == 1
        emit("east", b"diagnostic\n")
        assert control["flush_attempts"] == budgets["west"].sends == 2
        budgets["west"].budget = None
        observer.runtime.t_sim_ns = 250_000_000
        emit("east", b"diagnostic\n")
        assert control["completed"] and control["completed_logical_ns"] == 250_000_000
        assert peers["west"].recv(4096) == b"\xa5"
        assert control["flush_attempts"] == budgets["west"].sends == 3
        for name, sample, stamp in (("east", 3, 300_000_000), ("west", 2, 300_000_000),
                                    ("west", 3, 400_000_000)):
            observer.runtime.t_sim_ns = stamp
            emit_rows(name, sample)
        for name in ("east", "west"):
            emit(name, b"TTEST SKIP plant.physics 0 0\nTTEST PHASE 36 0 1\nTTEST DONE 36 0 1\n")
            assert report["cases"][name] == arena_rows(name)
            assert report["summaries"][name] == arena_summaries()
        assert watchdogs == [("east", "done"), ("west", "done")]
        assert budgets["west"].sends == 3 and budgets["east"].sends == 0
        assert all(value.reads == value.closes == 0 for value in budgets.values())
        with pytest.raises(ValueError, match="duplicate.*channel"):
            observer.channels(uarts={"uart1": observer.controls["west"]})
        assert len(channel_calls) == 2
    finally:
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("violation", ["late_baseline", "early_changed", "west_before_control",
                                      "foreign_stage", "stage_before_baseline", "bad_stage_values",
                                      "duplicate_stage", "duplicate_sample"])
def test_scenario_observer_rejects_bad_stage_early_or_duplicate_reports(referee_arena_manifest, violation):
    from sim.boards.channels import ChannelEvent

    report = {"uart": ["", ""], "ready": {}, "cases": {name: [] for name in ("east", "west")},
              "summaries": {name: [] for name in ("east", "west")},
              "scenario_observation": {"event_ns": 200_000_000, "samples": {"east": {}, "west": {}},
                  "stage": None, "control": None, "shared_start": None, "queue_calls": []}}
    observer = arena_module.RefereeObservation(report, referee_arena_manifest, scenario=True)
    observer.runtime = SimpleNamespace(t_sim_ns=50_000_000)
    def emit(name, data):
        observer.emit(arena_module.arena.ArenaEvent(name, ChannelEvent("uart1", 123, data)))
    if violation not in ("stage_before_baseline", "late_baseline"):
        rows = [row for row in arena_rows("west") if row["name"].startswith("referee.arena.1.")]
        emit("west", "".join(f"TTEST {row['status']} {row['name']} {row['actual']} {row['expected']}\n"
                              for row in rows).encode("ascii"))
    name, data = "west", b"TTEST READY referee.scenario.observe 0 0\n"
    if violation in ("late_baseline", "early_changed", "west_before_control", "duplicate_sample"):
        observer.runtime.t_sim_ns = 200_000_000 if violation == "late_baseline" else 50_000_000
        sample = 1 if violation in ("late_baseline", "duplicate_sample") else 2
        name = "east" if violation == "early_changed" else "west"
        data = f"TTEST PASS referee.arena.{sample}.fresh 1 1\n".encode("ascii")
    elif violation == "foreign_stage":
        name = "east"
    elif violation == "bad_stage_values":
        data = b"TTEST READY referee.scenario.observe 1 0\n"
    elif violation == "duplicate_stage":
        emit(name, data)
    with pytest.raises(ValueError, match="Scenario referee"):
        emit(name, data)


def arena_response_summary(wire_session, reset_qemu_ns):
    """Exact retained public ACK receipt; no transport implementation here."""
    from sim.transports import step_codec as codec
    def response(kind, step, target, dt, **decoded):
        return {"header": dict(kind=kind, flags=wire_session, step_id=step,
                               t_sim_ns=target, dt_ns=dt), "decoded": dict(status=0, **decoded)}
    return {"reset": response(codec.RESET_ACK, 0, 0, 0, virtual_time_ns=reset_qemu_ns),
            "step_ack_count": 1000,
            "first_step_ack": response(codec.STEP_ACK, 1, 1_000_000, 1_000_000),
            "last_step_ack": response(codec.STEP_ACK, 1000, 1_000_000_000, 1_000_000)}


@pytest.mark.parametrize("corruption", ["session", "reset_epoch", "count", "last_step", "status"])
def test_arena_ack_gate_requires_actual_scoped_identities(corruption):
    summary = arena_response_summary(2, 91_234)
    arena_module.check_referee_responses(summary, 2, 1000, 1_000_000_000, 91_234)
    if corruption == "session":
        summary["first_step_ack"]["header"]["flags"] = 1
    elif corruption == "reset_epoch":
        summary["reset"]["decoded"]["virtual_time_ns"] += 1
    elif corruption == "count":
        summary["step_ack_count"] -= 1
    elif corruption == "last_step":
        summary["last_step_ack"]["header"]["step_id"] -= 1
    else:
        summary["last_step_ack"]["decoded"]["status"] = 1
    with pytest.raises(ValueError, match="ACK .*disagrees"):
        arena_module.check_referee_responses(summary, 2, 1000, 1_000_000_000, 91_234)


@pytest.mark.parametrize("scenario", [False, True], ids=["arena", "scenario"])
def test_arena_cli_delegates_once_and_profile_flags_are_exclusive(tmp_path, monkeypatch, scenario):
    import builtins
    calls, imports = [], []
    original_import = builtins.__import__
    def observed_import(name, *args, **options):
        if name == "tools.dm_mc02_arena_smoke":
            imports.append(name)
        return original_import(name, *args, **options)
    monkeypatch.setattr(builtins, "__import__", observed_import)
    def admitted(elf, output, firmware, **options):
        calls.append((elf, output, firmware, options))
        return 23
    paths = tuple(str(tmp_path / name) for name in ("arena.elf", "report", "trobot"))
    monkeypatch.setattr(module, "run", lambda *args, **options: 17)
    assert module.main(list(paths)) == 17 and imports == [] and calls == []
    monkeypatch.setattr(arena_module, "run_referee", admitted)
    monkeypatch.setattr(module, "run", lambda *args, **options: pytest.fail("Arena dispatch reached old runner"))
    flag = "--scenario" if scenario else "--arena"
    assert module.main([*paths, flag, "--qemu", "/fixed/qemu"]) == 23
    assert calls == [(*paths, dict(qemu="/fixed/qemu", **({"scenario": True} if scenario else {})))]
    assert imports == ["tools.dm_mc02_arena_smoke"]
    with pytest.raises(SystemExit) as rejected:
        module.main([*paths, flag, "--shared-world"])
    assert rejected.value.code == 2 and len(calls) == 1
    if scenario:
        with pytest.raises(SystemExit) as rejected:
            module.main([*paths, "--scenario", "--arena"])
        assert rejected.value.code == 2 and len(calls) == 1


@pytest.mark.parametrize("corruption", ["cadence", "missing_due", "period", "ready", "admission", "sequence"])
def test_arena_publication_gate_checks_actual_period_and_complete_due_coverage(corruption):
    from dataclasses import replace
    ready, end = 25_000_000, 275_000_000
    rows = [dict(logical_ns=ready + index * 100_000_000, accepted=True, verified=True)
            for index in range(3)]
    referee = arena_module.arena.ArenaRefereeResult("east", "uart7", 100_000_000,
        3, ready, 225_000_000, 12)
    arena_module.check_referee_publications(rows, referee, end, ready)
    if corruption == "cadence":
        for index, row in enumerate(rows):
            row["logical_ns"] = ready + index * 50_000_000
        referee = replace(referee, last_logical_ns=rows[-1]["logical_ns"])
    elif corruption == "missing_due":
        rows.pop()
        referee = replace(referee, publications=2, last_logical_ns=rows[-1]["logical_ns"], next_sequence=8)
    elif corruption == "period":
        referee = replace(referee, period_ns=50_000_000)
    elif corruption == "ready":
        ready += 1_000_000
    elif corruption == "admission":
        rows[1]["verified"] = False
    else:
        referee = replace(referee, next_sequence=13)
    with pytest.raises(ValueError, match="quantities/cadence disagree"):
        arena_module.check_referee_publications(rows, referee, end, ready)


@pytest.mark.parametrize("resource", ["world", "qmp"])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_arena_observer_setup_failure_closes_acquired_owner_and_preserves_primary(
        referee_arena_manifest, resource, cleanup_failure):
    primary = ValueError("observer setup failed")
    cleanup = OSError("native owner cleanup failed")
    cleanup.add_note("native release detail")
    report = {"qmp_events": [], "qmp_cleanup": [], "native_calls": dict(reset=0, attempts=0,
        steps=0, total_ns=0, minimum_ns=None, maximum_ns=None, close=0)}
    observer = arena_module.RefereeObservation(report, referee_arena_manifest)
    if resource == "world":
        class SetupRejectingNative(NativeStub):
            def __setattr__(self, name, value):
                if name == "step" and getattr(self, "reject_setup", False):
                    raise primary
                super().__setattr__(name, value)
            def close(self):
                super().close()
                if cleanup_failure:
                    raise cleanup
        native = SetupRejectingNative()
        device = M3508Device(CanBus(), 1)
        acquired = M3508JointPlant(native, {device: "wheel_fl"})
        native.reject_setup = True
        observer.original_world = lambda manifest: acquired
        with pytest.raises(ValueError) as rejected:
            observer.world(referee_arena_manifest)
        assert acquired.closed and device.closed and native.closed == 1
        assert native.resets == 0 and native.steps == []
    else:
        class SetupRejectingQmp:
            def __init__(self):
                self.closes = 0
            @property
            def command(self):
                raise primary
            def close(self):
                self.closes += 1
                if cleanup_failure:
                    raise cleanup
        acquired = SetupRejectingQmp()
        observer.original_qmp = lambda *args, **options: acquired
        with pytest.raises(ValueError) as rejected:
            observer.qmp("unused-qmp-path")
        assert acquired.closes == 1
    assert rejected.value is primary
    notes = "\n".join(getattr(primary, "__notes__", ()))
    if cleanup_failure:
        assert "native owner cleanup failed" in notes and "native release detail" in notes


def test_arena_connect_observer_retains_actual_borrowed_socket_once(referee_arena_manifest):
    observer = arena_module.RefereeObservation({}, referee_arena_manifest)
    sender, peer = socket.socketpair()
    calls = []
    def connect(path, timeout):
        calls.append((path, timeout))
        return sender
    observer.original_connect = connect
    try:
        assert observer.connect("actual-uart1-path", .5) is sender
        assert calls == [("actual-uart1-path", .5)]
        assert observer.sockets == [("actual-uart1-path", sender)]
        assert sender.fileno() >= 0
    finally:
        sender.close()
        peer.close()


@pytest.fixture
def completed_arena_receipt(tmp_path, monkeypatch):
    """Isolate post-consumer verification using one completed Arena result.

    Lower runtime ownership and transport state machines have separate direct
    tests. This fixture never constructs or runs a second runtime/guest loop.
    """
    from tools import dm_mc02_referee_test as source_module

    elf = tmp_path / "fixture.elf"
    elf.write_bytes(b"fixed input")
    state = SimpleNamespace(mode="success", calls=[], events=[], snapshots=0)
    def snapshot(*args):
        state.snapshots += 1
        value = hashlib.sha256(elf.read_bytes()).hexdigest()
        return {str(elf): value + ("changed" if state.mode in ("source", "primary") and state.snapshots > 1 else "")}
    monkeypatch.setattr(source_module, "source_snapshot", snapshot)
    monkeypatch.setattr(source_module, "checkout_snapshot", lambda firmware: {})
    monkeypatch.setattr(arena_module, "source_origins", lambda: {})
    monkeypatch.setattr(arena_module, "starttime", lambda pid: None)
    write_json = source_module.write_json
    def observed_write(path, report):
        if report["result"] == "passed":
            state.events.append("published passed")
        write_json(path, report)
    monkeypatch.setattr(source_module, "write_json", observed_write)

    def completed(manifest, **options):
        state.calls.append((manifest, options.copy()))
        observer, report = options["emit"].__self__, options["emit"].__self__.report
        closed_socket = SimpleNamespace(fileno=lambda: -1)
        observer.sockets = [(str(tmp_path / f"removed-{index}.{name}"), closed_socket)
            for index in range(2) for name in ("uart1", "uart7", "can1", "cosim")]
        observer.engines = [SimpleNamespace(closed=True) for _ in range(2)]
        observer.endpoints = [SimpleNamespace(closed=True, transport=SimpleNamespace(sock=closed_socket))
                              for _ in range(2)]
        observer.plant = SimpleNamespace(closed=True, devices=[SimpleNamespace(closed=True) for _ in range(8)])
        observer.runtime = SimpleNamespace(closed=True, bindings=[SimpleNamespace(name=name,
            cosim=SimpleNamespace(sock=closed_socket), endpoint=SimpleNamespace(
                stream=SimpleNamespace(sock=closed_socket))) for name in ("east", "west")])
        observer.owners = [SimpleNamespace(process=SimpleNamespace(poll=lambda: 0)) for _ in range(2)]
        report["cleanup"] = [dict(pid=123456 + index, starttime=1,
            tempdir=str(tmp_path / f"removed-{index}"), sockets={"qmp": str(tmp_path / f"removed-{index}.qmp")})
            for index in range(2)]
        report["qmp_cleanup"] = [dict(attempts=1, completed=True) for _ in range(2)]
        report["native_calls"] = dict(reset=1, attempts=1000, steps=1000, total_ns=1_000_000_000,
            minimum_ns=1_000_000, maximum_ns=1_000_000, close=1)
        if report.get("scenario"):
            assert observer.scenario_manifest.arena is manifest
            observer.controls = {name: SimpleNamespace(sock=closed_socket) for name in ("east", "west")}
            batches = options["referee_events"]
            assert len(batches) == 2
            assert all(batch[0] == row.member and batch[1] is row.events
                       for batch, row in zip(batches, observer.scenario_manifest.referees))
            report["scenario_observation"] = scenario_observations()
            for row, batch in zip(report["scenario_observation"]["queue_calls"], batches):
                row["batch_identity"] = id(batch[1])
            if state.mode == "missing_control":
                report["scenario_observation"]["control"] = None
            elif state.mode == "late_baseline":
                report["scenario_observation"]["samples"]["west"]["1"]["logical_ns"] = 200_000_000
            elif state.mode == "scenario_source":
                observer.scenario_manifest.path.write_text("changed after admission\n")
        boards, referees = [], []
        for index, name in enumerate(("east", "west"), 1):
            report["cases"][name], report["summaries"][name] = arena_rows(name), arena_summaries()
            report["ready"][name] = dict(poll_sample_qemu_ns=100 * index, logical_ns=0)
            report["publications"][name] = [dict(logical_ns=period * 100_000_000, sequence=period * 4,
                accepted=True, verified=True) for period in range(11)]
            report["responses"][name].update(arena_response_summary(index, 90 * index))
            report["watchdogs"][name] = {stage: dict(timeouts=0, diagnostics="window-violations=0")
                                         for stage in ("done", "paused")}
            boards.append(arena_module.arena.ArenaBoardResult(name, "alpha/" if index == 1 else "bravo/",
                "uart_marker", 110 * index, 1_000_000_000 + index, 90 * index, 1000))
            referees.append(arena_module.arena.ArenaRefereeResult(name, "uart7", 100_000_000,
                11, 0, 1_000_000_000, 44))
        state.events.append("runtime cleanup completed")
        if state.mode == "missing_case":
            report["cases"]["west"].pop(0)
        elif state.mode == "missing_summary":
            report["summaries"]["west"].pop()
        elif state.mode == "missing_watchdog":
            del report["watchdogs"]["west"]["paused"]
        elif state.mode in ("cleanup", "primary"):
            report["qmp_cleanup"][1]["completed"] = False
        elif state.mode == "uart_socket":
            path, _ = observer.sockets[0]
            observer.sockets[0] = (path, SimpleNamespace(fileno=lambda: 3))
        if state.mode == "primary":
            raise KeyboardInterrupt("original Arena interruption")
        return arena_module.arena.ArenaResult(manifest.name, 0, 1_000_000_000, 1000,
            tuple(boards), (), referees=tuple(referees))
    monkeypatch.setattr(arena_module.arena, "run_manifest", completed)
    return elf, state


@pytest.mark.parametrize("mode,scenario", [
    (mode, False) for mode in ("success", "missing_case", "missing_summary", "missing_watchdog",
                              "cleanup", "uart_socket", "source", "primary")
] + [(mode, True) for mode in ("success", "missing_case", "cleanup", "source", "primary",
                              "missing_control", "late_baseline", "scenario_source")])
def test_arena_pass_requires_consumer_evidence_cleanup_and_preserved_sources(
        completed_arena_receipt, tmp_path, mode, scenario):
    elf, state = completed_arena_receipt
    state.mode = mode
    output = tmp_path / "result"
    expected = 0 if mode == "success" else 130 if mode == "primary" else 1
    assert arena_module.run_referee(elf, output, tmp_path, qemu=sys.executable, scenario=scenario) == expected
    report_path = output / "report.json"
    report = json.loads(report_path.read_text())
    assert len(state.calls) == 1
    options = state.calls[0][1]
    assert (options["duration"], options["startup_timeout"], options["timeout"]) == (1., 10., 20.)
    if scenario:
        assert report["scenario"] and report["scenario_input"]["duration_ns"] == 1_000_000_000
        assert [batch[0] for batch in options["referee_events"]] == ["east", "west"]
        path = report["scenario_input"]["path"]
        assert path in report["inputs_before"]
        if mode != "scenario_source":
            assert report["inputs_before"][path] == hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if mode == "success":
        assert report["result"] == "passed" and report["cleanup_verified"] and report["source_preserved"]
        assert report["missing"] == {"east": [], "west": []}
        assert state.events == ["runtime cleanup completed", "published passed"]
    else:
        assert report["result"] == "failed" and "published passed" not in state.events
        errors = "\n".join(report["errors"])
        assert {"missing_case": "coverage", "missing_summary": "summaries", "missing_watchdog": "watchdog",
                "cleanup": "cleanup incomplete", "uart_socket": "cleanup incomplete",
                "source": "source state changed", "primary": "interruption",
                "missing_control": "observation control", "late_baseline": "baseline/sample",
                "scenario_source": "source state changed"}[mode] in errors
        if mode == "missing_case":
            assert report["missing"]["west"] == ["control.rx"]
        if mode == "primary":
            assert report["error_type"] == "KeyboardInterrupt"
            assert report["errors"][0] == "KeyboardInterrupt: original Arena interruption"
            assert "cleanup incomplete" in "\n".join(report["notes"])
            assert "source state changed" in "\n".join(report["notes"])
    saved = report_path.read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        arena_module.run_referee(elf, output, tmp_path, qemu=sys.executable, scenario=scenario)
    assert report_path.read_bytes() == saved and len(state.calls) == 1
