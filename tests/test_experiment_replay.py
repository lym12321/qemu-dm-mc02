"""Offline evaluation of the finite experiment's actual ordered observations."""

from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("mcap")

from sim.api import Event, LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.apps.experiment import ExperimentInput, ExperimentReady, replay_experiment, run_experiment
from sim.config.scene import Scene
from sim.devices.uart_echo import UartEchoFactory
from sim.fabric import CanFrame
from sim.fabric.uart import UartNetFactory
from sim.trace.experiment import ExperimentTrace, iter_experiment_records
from test_sim_experiment import Peripheral, PeripheralFactory, scene, uart_assert


def capture(configured=None, registry=None, **options):
    if configured is None:
        configured, registry, _ = scene()
    output = BytesIO()
    result = run_experiment(configured, registry, trace=output, **options)
    assert not output.closed
    return output.getvalue(), result


@pytest.fixture(scope="module")
def recorded_trace():
    return capture()[0]


def rewrite(data, change):
    """Regenerate a valid lower container while changing its app lifecycle."""
    records = list(iter_experiment_records(BytesIO(data)))
    records = change(records)
    output = BytesIO()
    writer = ExperimentTrace(output, records[0].data)
    for record in records[1:]:
        if record.kind == "event":
            writer.event(record.data)
        elif record.kind == "cut":
            writer.cut(record.data)
        else:
            assert record.kind == "completion"
            writer.complete(record.data)
    return output.getvalue()


def test_recorded_binary_order_receipts_and_failed_assertions_replay_identically():
    data, online = capture(inputs=(
        ExperimentInput(3, "board.uart", b"N\0"), ExperimentInput(3, "board.uart", b"O"),
        ExperimentInput(4, "board.uart", b"K"), ExperimentInput(30, "board.uart", b"final")),
        assertions=(uart_assert("board.uart", "N\0OK"), uart_assert("board.uart", "absent")))
    source = BytesIO(data)
    offline = replay_experiment(source)
    assert offline == online
    assert offline.status == ("pass", "fail") and offline.delivered_inputs == 4
    assert (offline.matches[0].source_time_ns, offline.matches[0].mapped_time_ns,
            offline.matches[0].delivery_time_ns) == (704, 4, 4)
    assert not source.closed and source.tell() == len(data)
    records = list(iter_experiment_records(BytesIO(data)))
    assert [record.data.payload for record in records if record.kind == "event"
            and record.data.kind == "uart"] == [b"N\0", b"O", b"K", b"final"]


def test_replacement_assertions_use_all_recorded_uart_can_and_snapshot_capabilities():
    configured, registry, _ = scene(("source", "sink"), can=True)
    data, original = capture(configured, registry, inputs=(
        ExperimentInput(2, "source.level", 1), ExperimentInput(3, "source.uart", b"OK"),
        ExperimentInput(4, "source.can", CanFrame(0x321, data=b"\0\xff", timestamp_ns=4))))
    assert original.status == ()
    result = replay_experiment(BytesIO(data), (
        uart_assert("source.uart"),
        {"before": "20ns", "digital_equals": {"endpoint": "source.gpio.B0", "value": 1}},
        {"before": "20ns", "can_seen": {"endpoint": "source.can", "id": 0x321,
            "dlc": 2, "data_hex": "00ff"}}))
    assert result.status == ("pass", "pass", "pass")
    assert {receipt.kind for receipt in result.matches} == {"uart", "snapshot", "can.delivery"}


@pytest.mark.parametrize("machine,expected", [(False, "net.uart.delivery"), (True, "uart")])
def test_connected_replacement_preserves_delivery_versus_machine_observation(machine, expected):
    class Sender:
        def __init__(self, context):
            self.context = context
            self.connection = context.attach("uart", self)

        def start(self):
            self.connection.send(b"A", source_time_ns=701, mapped_time_ns=0, quality="upper_bound")
            self.context.emit("uart", "uart", b"A", source_time_ns=701,
                              mapped_time_ns=0, quality="upper_bound")

        def receive_uart(self, data):
            pass

        def prepare_step(self, context):
            pass

        def request_step(self, context):
            pass

        def finish_step(self, context):
            from sim.api import StepResult
            return StepResult("ok", context.t_sim_ns)

        def close(self):
            pass

    class Factory:
        def describe(self, spec):
            return NodeDescription((PortSpec("uart", "uart", min_connections=1),),
                                   {"machine"} if machine else ())

        def create(self, spec, context):
            return Sender(context)

    configured = Scene(Topology((NodeSpec("source", "sender"), NodeSpec("echo", "echo"),
        NodeSpec("wire", "wire")), (LinkSpec("source.uart", "wire.uart"),
        LinkSpec("echo.uart", "wire.uart"))), duration_ns=10, quantum_ns=5)
    data, _ = capture(configured, {"sender": Factory(), "echo": UartEchoFactory(),
                                  "wire": UartNetFactory()})
    result = replay_experiment(BytesIO(data), (uart_assert("source.uart", "A", "10ns"),
        uart_assert("source.uart", "AA", "10ns")))
    assert result.status == ("pass", "fail")
    assert result.matches[0].kind == expected


def test_ready_fragments_and_whole_ready_cut_exclusion_survive_replay():
    configured, registry, _ = scene(quantum=10, duration=20,
        options={"uart_observations": ((10, b"R"), (20, b"EADY startup"))})
    data, online = capture(configured, registry, ready=(ExperimentReady("main", "board.uart", "READY"),),
        inputs=(ExperimentInput(0, "board.uart", b"same-cut"),
                ExperimentInput(3, "board.uart", b"active")),
        assertions=(uart_assert("board.uart", "startup", "10ns"),
                    uart_assert("board.uart", "same-cut", "10ns"),
                    uart_assert("board.uart", "active", "10ns")))
    assert replay_experiment(BytesIO(data)) == online
    assert (online.origin_ns, online.end_ns, online.status) == (20, 40, ("fail", "fail", "pass"))
    assert online.ready_receipts["main"].source_time_ns == 720


@pytest.mark.parametrize("quantum,expected", [(10, "pass"), (5, "fail")])
def test_genuinely_empty_cut_expires_before_late_can_observation(quantum, expected):
    class Sparse(Peripheral):
        def sample(self, cut):
            if cut == 10:
                self.context.emit("can", "can-tx", CanFrame(0x123, timestamp_ns=704),
                    source_time_ns=704, mapped_time_ns=4, delivery_time_ns=10)

    class Factory(PeripheralFactory):
        def create(self, spec, context):
            instance = Sparse(spec, context, self)
            self.created.append(instance)
            return instance

    configured, _, _ = scene(quantum=quantum, duration=10)
    data, online = capture(configured, {"fixture": Factory()}, assertions=(
        {"before": "5ns", "can_seen": {"endpoint": "board.can", "id": 0x123}},))
    assert replay_experiment(BytesIO(data)) == online
    assert online.status == (expected,)
    records = list(iter_experiment_records(BytesIO(data)))
    assert [item.data for item in records if item.kind == "cut"] == (
        [0, 10] if quantum == 10 else [0, 5, 10])
    assert len([item for item in records if item.kind == "event"]) == 1


def test_same_cut_event_after_repeated_empty_markers_still_precedes_cut_expiry():
    configured, registry, _ = scene(quantum=5, duration=10)
    data, _ = capture(configured, registry, assertions=(
        {"before": "5ns", "can_seen": {"endpoint": "board.can", "id": 0x123}},))
    config = next(iter_experiment_records(BytesIO(data))).data
    output = BytesIO()
    writer = ExperimentTrace(output, config)
    writer.cut(0)
    writer.cut(5)
    writer.cut(5)
    writer.event(Event(1, 0, "board", "can", "can-tx", CanFrame(0x123, timestamp_ns=704),
                       704, 4, 5, "exact"))
    writer.cut(5)
    writer.cut(10)
    writer.complete({"end_ns": 10, "origin_ns": 0, "delivered_inputs": 0,
                     "events_total": 1, "cuts_total": 5, "ready_receipts": {}, "boards": {}})
    result = replay_experiment(BytesIO(output.getvalue()))
    assert result.status == ("pass",)
    assert result.matches[0].event_sequence == 0
    assert (result.matches[0].source_time_ns, result.matches[0].mapped_time_ns,
            result.matches[0].delivery_time_ns) == (704, 4, 5)


def test_large_assertion_metadata_and_maximum_nodes_replay_offline(monkeypatch):
    configured, registry, factory = scene(tuple(f"node{index}" for index in range(63)), duration=10)
    assertions = tuple(uart_assert(f"node{index % 63}.uart", "marker" * 20, "10ns")
                       for index in range(1024))
    data, online = capture(configured, registry, assertions=assertions)
    assert len(data) > 65536 and len(factory.closed) == 63

    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay used native/factory/firmware access")

    monkeypatch.setattr(PeripheralFactory, "describe", forbidden)
    monkeypatch.setattr(PeripheralFactory, "create", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert replay_experiment(BytesIO(data)) == online


@pytest.mark.parametrize("replacement", [
    (uart_assert("missing.uart"),), (uart_assert("board.level"),),
    (uart_assert("board.uart", before="31ns"),), (uart_assert("board.uart"),) * 1025,
])
def test_replacement_admission_reuses_original_grammar_and_bounds(recorded_trace, replacement):
    with pytest.raises((TypeError, ValueError)):
        replay_experiment(BytesIO(recorded_trace), replacement)


def test_replacement_cannot_hide_invalid_recorded_original_assertions(recorded_trace):
    def change(records):
        records[0] = replace(records[0], data={**records[0].data,
            "assertions": (uart_assert("missing.uart"),)})
        return records

    with pytest.raises(ValueError, match="unknown assertion node"):
        replay_experiment(BytesIO(rewrite(recorded_trace, change)), ())


@pytest.mark.parametrize("field,value", [
    ("duration_ns", 0), ("quantum_ns", True), ("connected", ("missing.uart",)),
    ("descriptions", {}), ("assertions", (uart_assert("board.uart"),) * 1025),
    ("ready", {"endpoint": "board.can", "text": b"READY"}),
])
def test_recorded_configuration_is_admitted_without_runtime(recorded_trace, field, value):
    def change(records):
        records[0] = replace(records[0], data={**records[0].data, field: value})
        return records

    with pytest.raises((TypeError, ValueError)):
        replay_experiment(BytesIO(rewrite(recorded_trace, change)))


@pytest.mark.parametrize("changes", [
    {"origin": "marker:undeclared"}, {"origin": "unsupported"},
    {"at_ns": 31}, {"endpoint": "missing.uart"}, {"endpoint": "board.snapshot"},
])
def test_recorded_inputs_require_declared_timeline_horizon_and_input_capability(changes):
    data, _ = capture(inputs=(ExperimentInput(3, "board.uart", b"opaque payload"),))

    def change(records):
        config = records[0].data
        records[0] = replace(records[0], data={**config,
            "inputs": ({**config["inputs"][0], **changes},)})
        return records

    with pytest.raises((TypeError, ValueError)):
        replay_experiment(BytesIO(rewrite(data, change)))


@pytest.mark.parametrize("field,value", [
    ("events_total", 99), ("cuts_total", 99), ("end_ns", 29),
    ("origin_ns", 1), ("delivered_inputs", 1),
])
def test_valid_container_with_false_application_completion_is_rejected(recorded_trace, field, value):
    def change(records):
        records[-1] = replace(records[-1], data={**records[-1].data, field: value})
        return records

    with pytest.raises(ValueError, match="completion differs"):
        replay_experiment(BytesIO(rewrite(recorded_trace, change)))


@pytest.mark.parametrize("changes", [{"source_node": "missing"}, {"source_port": "missing"}])
def test_event_source_requires_captured_node_and_port(recorded_trace, changes):
    def change(records):
        index = next(index for index, item in enumerate(records) if item.kind == "event")
        records[index] = replace(records[index], data=replace(records[index].data, **changes))
        return records

    with pytest.raises(ValueError, match="unknown"):
        replay_experiment(BytesIO(rewrite(recorded_trace, change)))


def test_missing_physical_sample_marker_is_rejected_even_with_consistent_counts(recorded_trace):
    def change(records):
        result = [item for item in records if not (item.kind == "cut" and item.data == 10)]
        result[-1] = replace(result[-1], data={**result[-1].data,
                                             "cuts_total": result[-1].data["cuts_total"] - 1})
        return result

    with pytest.raises(ValueError, match="observation cut"):
        replay_experiment(BytesIO(rewrite(recorded_trace, change)))


@pytest.mark.parametrize("damage", ["truncated", "trailing", "crc"])
def test_no_result_before_complete_container_validation(recorded_trace, damage):
    data = recorded_trace
    if damage == "truncated":
        data = data[:-12]
    elif damage == "trailing":
        data += b"trailing"
    else:
        index = data.index(b"sample")
        data = data[:index] + bytes([data[index] ^ 1]) + data[index + 1:]
    with pytest.raises((ValueError, RuntimeError)):
        replay_experiment(BytesIO(data))


def test_stream_is_borrowed_bounded_and_read_once_without_seek_or_tell():
    data, online = capture()

    class Borrowed:
        def __init__(self, data):
            self.source = BytesIO(data)
            self.bytes_read = self.eofs = 0
            self.closed = False

        def read(self, size):
            assert 0 <= size < 70000
            assert self.eofs == 0
            result = self.source.read(size)
            self.bytes_read += len(result)
            self.eofs += not result
            return result

        def close(self):
            self.closed = True

    source = Borrowed(data)
    assert replay_experiment(source) == online
    assert source.bytes_read == len(data) and source.eofs == 1 and not source.closed
    with pytest.raises(TypeError, match="callable read"):
        replay_experiment(object())


@pytest.mark.parametrize("at_eof", [False, True])
def test_replay_read_errors_propagate_without_close_or_retry(recorded_trace, at_eof):
    primary = OSError("borrowed input diagnostic")

    class Input(BytesIO):
        failed = False

        def read(self, size):
            assert not self.failed
            if not at_eof or super().tell() == len(recorded_trace):
                self.failed = True
                raise primary
            return super().read(size)

    source = Input(recorded_trace)
    with pytest.raises(OSError) as caught:
        replay_experiment(source)
    assert caught.value is primary and source.failed and not source.closed


@pytest.mark.parametrize("failure", ["runtime", "cleanup", "both", "write", "flush"])
def test_failure_retains_primary_closes_owned_components_and_returns_no_result(failure):
    configured, registry, factory = scene(("board", "peer"))
    primary = KeyboardInterrupt("primary input")
    if failure in ("runtime", "both"):
        factory.input_error = primary
    if failure in ("cleanup", "both"):
        factory.close_error = OSError("cleanup diagnostic")

    class Output(BytesIO):
        fail_write = False

        def write(self, data):
            if self.fail_write:
                raise OSError("write diagnostic")
            return super().write(data)

        def flush(self):
            if failure == "flush":
                assert set(factory.closed) == set(factory.created)
                raise OSError("flush diagnostic")
            return super().flush()

    output = Output()

    def observed(event):
        if failure == "write":
            output.fail_write = True

    with pytest.raises((KeyboardInterrupt, OSError)) as caught:
        run_experiment(configured, registry, trace=output, on_event=observed,
                       inputs=(ExperimentInput(3, "board.uart", b"OK"),),
                       assertions=(uart_assert("board.uart"),))
    assert not output.closed and set(factory.closed) == set(factory.created)
    if failure in ("runtime", "both"):
        assert caught.value is primary
    if failure == "both":
        assert any("cleanup diagnostic" in note for note in primary.__notes__)
    if failure != "flush":
        with pytest.raises(ValueError):
            replay_experiment(BytesIO(output.getvalue()))


def test_recording_admission_and_initial_write_failure_precede_resources():
    configured, registry, factory = scene()
    output = BytesIO()
    with pytest.raises(ValueError, match="unknown assertion node"):
        run_experiment(configured, registry, trace=output, assertions=(uart_assert("missing.uart"),))
    assert output.getvalue() == b"" and factory.created == []

    class Broken(BytesIO):
        def write(self, data):
            raise OSError("initial write failed")

    broken = Broken()
    with pytest.raises(OSError, match="initial write"):
        run_experiment(configured, registry, trace=broken)
    assert factory.created == [] and not broken.closed


def test_optional_dependency_is_lazy_and_missing_extra_has_backend_status(tmp_path):
    script = """import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'mcap' or name.startswith('mcap.'):
        raise ModuleNotFoundError('blocked mcap', name=name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from io import BytesIO
from sim.api import BackendUnavailable
from sim.apps.experiment import run_experiment, replay_experiment
from sim.api import NodeDescription, NodeSpec, Topology
from sim.config.scene import Scene
class Component:
    def close(self):
        pass
class Factory:
    def describe(self, spec):
        return NodeDescription()
    def create(self, spec, context):
        return Component()
result = run_experiment(Scene(Topology((NodeSpec('fixture', 'fixture'),)), duration_ns=10),
                        {'fixture': Factory()})
assert result.passed and result.end_ns == 10
assert not any(name == 'mcap' or name.startswith('mcap.') for name in sys.modules)
for action in (lambda: replay_experiment(BytesIO()), lambda: run_experiment(None, {}, trace=BytesIO())):
    try:
        action()
    except BackendUnavailable as error:
        assert 'trace extra' in str(error)
    else:
        raise AssertionError('missing optional dependency was accepted')
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def machine_recording():
    """Explicit mock machine observations; these are not native QEMU evidence."""
    from sim.api import StepResult
    from test_sim_experiment import snapshot

    class Machine(Peripheral):
        def prepare_step(self, context):
            pass

        def request_step(self, context):
            pass

        def finish_step(self, context):
            return StepResult("ok", context.t_sim_ns)

        def sample(self, cut):
            native = self.spec.options["epoch"] + cut
            self.context.emit("snapshot", "snapshot", snapshot(native, self.level),
                              source_time_ns=native, mapped_time_ns=cut)
            for stamp, data in self.spec.options.get("uart_observations", ()):
                if stamp == cut:
                    self.context.emit("uart", "uart", data, source_time_ns=native,
                                      mapped_time_ns=cut, quality="upper_bound")

    class Factory(PeripheralFactory):
        def describe(self, spec):
            description = super().describe(spec)
            return NodeDescription(description.ports, description.capabilities | {"machine"})

        def create(self, spec, context):
            instance = Machine(spec, context, self)
            self.created.append(instance)
            return instance

    configured = Scene(Topology((
        NodeSpec("west", "mock", {"epoch": 700, "uart_observations": ((5, b"WEST startup"),)}),
        NodeSpec("east", "mock", {"epoch": 900, "uart_observations": ((10, b"EAST"),)}),
    )), quantum_ns=5, duration_ns=20)
    factory = Factory()
    return capture(configured, {"mock": factory},
        ready=(ExperimentReady("west", "west.uart", "WEST"),
               ExperimentReady("east", "east.uart", "EAST")),
        inputs=(ExperimentInput(1, "west.level", 1, "marker:west"),),
        assertions=({"origin": "session", **uart_assert("west.uart", "startup", "10ns")},
                    uart_assert("west.uart", "startup", "10ns")))


def test_multiple_ready_receipts_native_epochs_empty_batches_and_origins_survive_replay(machine_recording):
    data, online = machine_recording
    assert replay_experiment(BytesIO(data)) == online
    assert (online.origin_ns, online.end_ns, online.status) == (10, 30, ("pass", "fail"))
    assert list(online.ready_receipts) == ["west", "east"]
    assert [item.source_time_ns for item in online.ready_receipts.values()] == [705, 910]
    assert online.boards["west"] == {"native_origin_ns": 700, "first_source_ns": 700,
        "last_source_ns": 730, "first_mapped_ns": 0, "last_mapped_ns": 30,
        "observation_batches": 8}
    assert online.boards["east"]["native_origin_ns"] == 900
    assert online.boards["east"]["observation_batches"] == 8
    # Only two UART batches, but every native snapshot and empty cut remains.
    records = list(iter_experiment_records(BytesIO(data)))
    assert sum(record.kind == "event" and record.data.kind == "uart" for record in records) == 2
    assert [record.data for record in records if record.kind == "cut"] == [0, 5, 6, 10, 15, 20, 25, 30]
    replacement = replay_experiment(BytesIO(data), (
        {"origin": "session", **uart_assert("east.uart", "EAST", "15ns")},
        uart_assert("east.uart", "EAST", "15ns")))
    assert replacement.status == ("pass", "fail")


@pytest.mark.parametrize("field", ["ready_receipts", "boards"])
def test_completion_cannot_forge_ready_receipt_or_native_epoch_metadata(machine_recording, field):
    data, _ = machine_recording
    def change(records):
        records[-1] = replace(records[-1], data={**records[-1].data, field: {}})
        return records
    with pytest.raises(ValueError, match="captured lifecycle"):
        replay_experiment(BytesIO(rewrite(data, change)))


def test_native_epoch_change_is_rejected_even_with_matching_completion_and_valid_container(machine_recording):
    data, _ = machine_recording
    def change(records):
        for index, record in enumerate(records):
            if record.kind == "event" and record.data.kind == "snapshot" and record.data.mapped_time_ns == 30:
                event = record.data
                payload = {**event.payload, "timestamp_ns": event.source_time_ns + 1}
                records[index] = replace(record, data=replace(event,
                    source_time_ns=event.source_time_ns + 1, payload=payload))
                break
        return records
    with pytest.raises(ValueError, match="epoch changed"):
        replay_experiment(BytesIO(rewrite(data, change)))
