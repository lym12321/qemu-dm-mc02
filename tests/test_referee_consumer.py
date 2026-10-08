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
REAL_CONSUME = module.consume

from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from test_sim_shared_world import NativeStub

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
