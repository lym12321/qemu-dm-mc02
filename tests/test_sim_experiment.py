"""Finite experiment consumers of actual public Simulation Events and cuts."""

from dataclasses import asdict
from pathlib import Path
import json

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.apps.experiment import ExperimentInput, ExperimentReady, run_experiment
from sim.config.scene import ExperimentSpec, Scene
from sim.fabric import CanFrame
from sim.fabric.factory import CanBusFactory
from sim.fabric.uart import UartNetFactory


def snapshot(timestamp, level):
    # Independent literal native public schema, without MMIO or invented state.
    return {"schema": 1, "timestamp_ns": timestamp, "board": "fixture", "vin_mv": 24000,
        "gpio": {bank: {"moder": 0, "sampled": level if bank == "B" else 0,
                       "odr": 0, "output_mask": 0} for bank in "ABCDEFGH"},
        "key": {"pin": "A15", "pressed": False, "level": level},
        "led": {"pin": "A7", "rgb": 0, "brightness": 0, "source": "gpio-latch"}}


class Peripheral:
    def __init__(self, spec, context, factory):
        self.spec, self.context, self.factory = spec, context, factory
        self.level = 0
        self.calls = []
        if "can" in context.connected_ports:
            self.bus = context.attach("can", self)

    def receive(self, frame):
        self.calls.append(("receive", frame))

    def input(self, port, payload):
        self.calls.append((self.context.t_sim_ns, port, payload))
        if port == "uart":
            self.context.emit(port, "uart", payload,
                source_time_ns=700 + self.context.t_sim_ns,
                mapped_time_ns=self.context.t_sim_ns, quality="upper_bound")
        elif port == "level":
            self.level = payload
        elif port == "can":
            self.bus.send(payload)
        if self.factory.input_error:
            raise self.factory.input_error

    def sample(self, cut):
        self.context.emit("snapshot", "snapshot", snapshot(700 + cut, self.level),
            source_time_ns=700 + cut, mapped_time_ns=cut, quality="exact")
        for stamp, frame in self.spec.options.get("can_observations", ()):
            if stamp == cut:
                self.context.emit("can", "can-tx", frame, source_time_ns=frame.timestamp_ns,
                                  mapped_time_ns=frame.timestamp_ns - 700, delivery_time_ns=cut)
        for stamp, data in self.spec.options.get("uart_observations", ()):
            if stamp == cut:
                self.context.emit("uart", "uart", data, source_time_ns=700 + cut,
                                  mapped_time_ns=cut, quality="upper_bound")

    def close(self):
        self.factory.closed.append(self)
        if self.factory.close_error:
            raise self.factory.close_error


class PeripheralFactory:
    def __init__(self):
        self.created, self.closed = [], []
        self.input_error = self.close_error = None

    def describe(self, spec):
        return NodeDescription((
            PortSpec("uart", "uart", capabilities={"external_input"}),
            PortSpec("level", "digital", "in", "signal", capabilities={"external_input"}),
            PortSpec("can", "can", capabilities={"external_input"}),
            PortSpec("snapshot", "snapshot", "out", "signal"),
        ), {"sample", "input"})

    def validate_input(self, spec, port, value, *, connected_ports=()):
        if port == "uart" and type(value) is bytes and len(value) <= 4096:
            return value
        if port == "level" and type(value) is int and value in (0, 1):
            return value
        if port == "can" and isinstance(value, CanFrame):
            return value
        raise ValueError("invalid fixture input")

    def create(self, spec, context):
        instance = Peripheral(spec, context, self)
        self.created.append(instance)
        return instance


def scene(nodes=("board",), *, quantum=10, duration=30, options=None, can=False):
    factory = PeripheralFactory()
    specs = tuple(NodeSpec(name, "fixture", options or {}) for name in nodes)
    links = ()
    registry = {"fixture": factory}
    if can:
        specs += (NodeSpec("bus", "bus"),)
        links = tuple(LinkSpec(f"{name}.can", "bus.can") for name in nodes)
        registry["bus"] = CanBusFactory()
    return Scene(Topology(specs, links), quantum_ns=quantum, duration_ns=duration), registry, factory


def uart_assert(endpoint, text="OK", before="20ns"):
    return {"before": before, "uart_contains": {"endpoint": endpoint, "text": text}}


def test_single_peripheral_binary_fragments_and_equal_time_stimuli_use_one_runtime(tmp_path):
    factory = PeripheralFactory()
    path = tmp_path / "scene.yaml"
    path.write_text("schema: sim/v1\nruntime: {quantum: 10ns, duration: 30ns}\n"
                    "nodes: {port: {kind: fixture}}\n")
    events = []
    result = run_experiment(path, {"fixture": factory}, inputs=(
        ExperimentInput(3, "port.uart", b"N\0"), ExperimentInput(3, "port.uart", b"O"),
        ExperimentInput(4, "port.uart", b"K"), ExperimentInput(30, "port.uart", b"final")),
        assertions=(uart_assert("port.uart", "N\0OK"),), on_event=events.append)
    assert result.passed and result.status == ("pass",)
    assert result.end_ns == 30 and result.delivered_inputs == 4
    assert [call for call in factory.created[0].calls] == [
        (3, "uart", b"N\0"), (3, "uart", b"O"), (4, "uart", b"K"), (30, "uart", b"final")]
    assert factory.closed == factory.created
    receipt = result.matches[0]
    event = next(event for event in events if event.sequence == receipt.event_sequence)
    assert (receipt.source_time_ns, receipt.mapped_time_ns, receipt.delivery_time_ns,
            receipt.quality) == (704, 4, 4, "upper_bound")
    assert event.payload == b"K" and result.events_total == len(events)
    json.dumps(asdict(result))


def test_single_board_projection_matches_actual_snapshot_after_input_cut():
    configured, registry, factory = scene()
    result = run_experiment(configured, registry, inputs=(ExperimentInput(3, "board.level", 1),),
        assertions=({"before": "20ns", "digital_equals": {"endpoint": "board.gpio.B0", "value": 1}},))
    assert result.passed and result.matches[0].kind == "snapshot"
    assert result.matches[0].mapped_time_ns == 13  # Next existing cut after input3.
    assert result.matches[0].source_time_ns == 713
    assert factory.closed == factory.created


def test_multiboard_assertions_keep_endpoint_identity_and_native_epochs():
    configured, registry, factory = scene(("east", "west"))
    result = run_experiment(configured, registry, inputs=(
        ExperimentInput(2, "west.uart", b"eastOK"), ExperimentInput(4, "east.uart", b"OK"),
        ExperimentInput(5, "west.uart", b"west")), assertions=(
        uart_assert("east.uart"), uart_assert("west.uart", "west")))
    assert result.status == ("pass", "pass")
    assert [(receipt.source_node, receipt.mapped_time_ns) for receipt in result.matches] == [
        ("east", 4), ("west", 5)]
    assert len(factory.created) == len(factory.closed) == 2


@pytest.mark.parametrize("wire_time,expected", [(5, "pass"), (25, "fail")])
def test_can_assertion_observes_actual_fabric_delivery_and_ignores_future_queue(wire_time, expected):
    configured, registry, factory = scene(("source", "sink"), can=True)
    result = run_experiment(configured, registry,
        inputs=(ExperimentInput(5, "source.can", CanFrame(0x123, data=b"\x01\x02", timestamp_ns=wire_time)),),
        assertions=({"before": "10ns", "can_seen": {"endpoint": "source.can", "id": 0x123,
                     "flags": 0, "dlc": 2, "data_hex": "0102"}},))
    assert result.status == (expected,)
    if expected == "pass":
        assert result.matches[0].kind == "can.delivery"
    assert factory.created[1].calls[0] == (
        "receive", CanFrame(0x123, data=b"\x01\x02", timestamp_ns=wire_time))


def test_machine_can_assertion_retains_native_epoch_after_network_send_event():
    class Machine(Peripheral):
        def prepare_step(self, ctx):
            pass

        def request_step(self, ctx):
            pass

        def finish_step(self, ctx):
            if ctx.step_id == 1:
                self.bus.send(CanFrame(0x123, data=b"x", timestamp_ns=4))
                self.context.emit("can", "can-tx", CanFrame(0x123, data=b"x", timestamp_ns=704),
                    source_time_ns=704, mapped_time_ns=4, delivery_time_ns=ctx.t_sim_ns)
            return StepResult("ok", ctx.t_sim_ns)

    class MachineFactory(PeripheralFactory):
        def describe(self, spec):
            return NodeDescription(super().describe(spec).ports, {"machine", "input"})

        def create(self, spec, context):
            instance = Machine(spec, context, self)
            self.created.append(instance)
            return instance

    configured, registry, _ = scene(("board", "sink"), can=True)
    configured = Scene(Topology((NodeSpec("board", "machine"),
        *configured.topology.nodes[1:]), configured.topology.links), quantum_ns=10, duration_ns=30)
    registry["machine"] = MachineFactory()
    events = []
    result = run_experiment(configured, registry, on_event=events.append,
        assertions=({"before": "20ns", "can_seen": {"endpoint": "board.can", "id": 0x123}},))
    receipt = result.matches[0]
    sent = next(event for event in events if event.kind == "can.sent")
    assert result.passed and receipt.kind == "can-tx" and sent.sequence < receipt.event_sequence
    assert sent.source_time_ns == 4
    assert (receipt.source_time_ns, receipt.mapped_time_ns, receipt.delivery_time_ns,
            receipt.quality) == (704, 4, 10, "exact")


@pytest.mark.parametrize("mapped,expected", [(0, "pass"), (7, "pass"), (15, "fail")])
def test_connected_uart_observes_delivery_once_and_ignores_future_queue(mapped, expected):
    from sim.devices.uart_echo import UartEchoFactory

    class Sender:
        def __init__(self, _spec, context):
            self.context = context
            self.connection = context.attach("uart", self)

        def start(self):
            self.connection.send(b"A", source_time_ns=701, mapped_time_ns=mapped, quality="upper_bound")
            # The machine adapter also emits its own copy for direct observers.
            self.context.emit("uart", "uart", b"A", source_time_ns=701,
                              mapped_time_ns=mapped, quality="upper_bound")

        def receive_uart(self, data):
            pass

        def close(self):
            pass

    class SenderFactory:
        def describe(self, spec):
            return NodeDescription((PortSpec("uart", "uart", min_connections=1),))

        def create(self, spec, context):
            return Sender(spec, context)

    configured = Scene(Topology((NodeSpec("source", "sender"), NodeSpec("echo", "echo"),
        NodeSpec("wire", "wire")), (LinkSpec("source.uart", "wire.uart"),
                                    LinkSpec("echo.uart", "wire.uart"))), duration_ns=10, quantum_ns=5)
    result = run_experiment(configured, {"sender": SenderFactory(), "echo": UartEchoFactory(),
        "wire": UartNetFactory()}, assertions=(uart_assert("source.uart", "AA", "10ns"),
                                               uart_assert("echo.uart", "A", "10ns")))
    assert result.status == ("fail", expected)
    if expected == "pass":
        assert result.matches[0].kind == "net.uart.delivery"
        assert result.matches[0].mapped_time_ns == mapped


@pytest.mark.parametrize("quantum,expected", [(10, "pass"), (5, "fail")])
def test_wire_time_matches_before_expiry_but_never_recovers_failed_empty_cut(quantum, expected):
    configured, registry, _ = scene(quantum=quantum, duration=10,
        options={"can_observations": ((10, CanFrame(0x123, data=b"x", timestamp_ns=704)),)})
    result = run_experiment(configured, registry,
        assertions=({"before": "5ns", "can_seen": {"endpoint": "board.can", "id": 0x123}},))
    assert result.status == (expected,)
    if expected == "pass":
        assert result.matches[0].source_time_ns == 704
        assert result.matches[0].mapped_time_ns == 4 and result.matches[0].delivery_time_ns == 10


def test_can_lower_bound_cannot_prove_strict_before_assertion():
    from sim.api import Event
    from sim.apps.experiment import _Consumer
    from sim.lab.manifest import parse_assertion

    assertion = parse_assertion({"before": "5ns", "can_seen": {
        "endpoint": "board.can", "id": 0x123}}, 0,
        endpoint_validator=lambda _kind, endpoint: endpoint)
    consumer = _Consumer((assertion,), 10, {}, {"board.can": "can-tx"})
    with pytest.raises(ValueError, match="upper bound"):
        consumer(Event(1, 0, "board", "can", "can-tx", CanFrame(0x123, timestamp_ns=901),
                       901, 1, 10, "lower_bound"))
    assert consumer.status == ("pending",) and consumer.matches == []


def test_strict_uart_deadline_final_empty_cut_and_bounded_history_do_not_hide_failures():
    configured, registry, factory = scene()
    result = run_experiment(configured, registry,
        inputs=(ExperimentInput(5, "board.uart", b"OK"),),
        assertions=(uart_assert("board.uart", before="5ns"), uart_assert("board.uart", "absent")))
    assert result.status == ("fail", "fail") and len(result.failures) == 2
    assert result.matches == () and factory.closed == factory.created


@pytest.mark.parametrize("assertions,inputs", [
    ((uart_assert("missing.uart"),), ()),
    ((uart_assert("board.level"),), ()),
    (({"before": "10ns", "digital_equals": {"endpoint": "board.level", "value": 1}},), ()),
    ((uart_assert("board.uart", before="31ns"),), ()),
    ((), (ExperimentInput(31, "board.uart", b"too late"),)),
    ((), (ExperimentInput(3, "board.level", 2),)),
])
def test_bad_experiment_admission_precedes_all_resource_creation(assertions, inputs):
    configured, registry, factory = scene()
    with pytest.raises((TypeError, ValueError)):
        run_experiment(configured, registry, assertions=assertions, inputs=inputs)
    assert factory.created == factory.closed == []


def test_observation_node_limit_is_admitted_before_resources():
    configured, registry, factory = scene(tuple(f"node{index}" for index in range(64)))
    with pytest.raises(ValueError, match="at most 63 nodes"):
        run_experiment(configured, registry)
    assert factory.created == factory.closed == []


def test_input_primary_survives_cleanup_and_every_owned_component_is_closed():
    configured, registry, factory = scene(("a", "b"))
    primary = KeyboardInterrupt("primary input interrupt")
    factory.input_error, factory.close_error = primary, OSError("close diagnostic")
    with pytest.raises(KeyboardInterrupt) as caught:
        run_experiment(configured, registry, inputs=(ExperimentInput(3, "a.uart", b"x"),))
    assert caught.value is primary and len(factory.closed) == 2
    assert any("close diagnostic" in note for note in primary.__notes__)


def test_cleanup_failure_prevents_returning_a_passed_result():
    configured, registry, factory = scene()
    primary = factory.close_error = OSError("cleanup failed")
    with pytest.raises(OSError) as caught:
        run_experiment(configured, registry, inputs=(ExperimentInput(3, "board.uart", b"OK"),),
                       assertions=(uart_assert("board.uart"),))
    assert caught.value is primary and factory.closed == factory.created


def test_optional_legacy_runners_and_backends_are_not_imported(tmp_path):
    import os
    import subprocess
    import sys

    script = """import sys
from sim.apps.experiment import run_experiment
for name in sys.modules:
    assert not name.startswith(('tools.', 'sim.arena', 'sim.lab.runtime', 'mujoco', 'mcap', 'qemu.')), name
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_ready_fragments_set_relative_origin_and_exclude_the_whole_matching_cut():
    configured, registry, factory = scene(quantum=10, duration=20,
        options={"uart_observations": ((10, b"R"), (20, b"EADY startup"))})
    events = []
    result = run_experiment(configured, registry, ready=(ExperimentReady("main", "board.uart", "READY"),),
        inputs=(ExperimentInput(0, "board.uart", b"same-cut"),
                ExperimentInput(3, "board.uart", b"active")),
        assertions=(uart_assert("board.uart", "startup", "10ns"),
                    uart_assert("board.uart", "same-cut", "10ns"),
                    uart_assert("board.uart", "active", "10ns")), on_event=events.append)
    assert result.origin_ns == 20 and result.end_ns == 40
    assert result.status == ("fail", "fail", "pass") and result.delivered_inputs == 2
    assert factory.created[0].calls == [(20, "uart", b"same-cut"), (23, "uart", b"active")]
    receipt = result.ready_receipts["main"]
    original = next(event for event in events if event.sequence == receipt.event_sequence)
    assert original.payload == b"EADY startup" and original.session_id == 1
    assert (receipt.source_time_ns, receipt.mapped_time_ns, receipt.delivery_time_ns,
            receipt.quality) == (720, 20, 20, "upper_bound")
    assert result.matches[0].source_time_ns == 723
    assert result.matches[0].mapped_time_ns == 23
    assert factory.closed == factory.created
    json.dumps(asdict(result))


def test_ready_at_zero_defers_enqueue_until_start_completes():
    configured, registry, factory = scene(duration=10,
        options={"uart_observations": ((0, b"READY"),)})
    result = run_experiment(configured, registry, ready=(ExperimentReady("main", "board.uart", b"READY"),),
        inputs=(ExperimentInput(0, "board.level", 1),),
        assertions=({"before": "10ns", "digital_equals": {"endpoint": "board.gpio.B0", "value": 1}},))
    assert result.origin_ns == 0 and result.ready_receipts["main"].mapped_time_ns == 0
    assert factory.created[0].calls == [(0, "level", 1)]
    # The initial matching cut is excluded; a later completed sample is needed.
    assert result.status == ("fail",) and result.delivered_inputs == 1


def test_ready_zero_offset_input_reaches_input_commit_before_target_commit():
    class CommitPeripheral(Peripheral):
        def __init__(self, *args):
            super().__init__(*args)
            self.commits = []

        def commit_inputs(self, cut):
            self.commits.append((cut, self.level))

    class CommitFactory(PeripheralFactory):
        def describe(self, spec):
            return NodeDescription(super().describe(spec).ports, {"sample", "input", "input_commit"})

        def create(self, spec, context):
            instance = CommitPeripheral(spec, context, self)
            self.created.append(instance)
            return instance

    factory = CommitFactory()
    configured = Scene(Topology((NodeSpec("board", "fixture", {
        "uart_observations": ((10, b"READY"),)}),)), quantum_ns=10, duration_ns=10)
    result = run_experiment(configured, {"fixture": factory},
        ready=(ExperimentReady("main", "board.uart", "READY"),), inputs=(ExperimentInput(0, "board.level", 1),))
    assert result.passed and result.origin_ns == 10 and result.end_ns == 20
    assert factory.created[0].calls == [(10, "level", 1)]
    assert (10, 1) in factory.created[0].commits


def test_ready_wrong_endpoint_never_joins_fragments_and_tail_stays_bounded():
    from sim.api import Event
    from sim.apps.experiment import _Consumer

    consumer = _Consumer((), 10, {}, {"board.uart": "uart", "peer.uart": "uart"},
                         ready=(ExperimentReady("main", "board.uart", "READY"),))
    def observe(index, node, data):
        consumer(Event(1, index, node, "uart", "uart", data, 700 + index,
                       index, index, "upper_bound"))
    observe(0, "board", b"x" * 4096 + b"RE")
    assert len(consumer._ready_tails["main"]) == 4
    observe(1, "peer", b"ADY")
    assert consumer.origin_ns is None
    observe(2, "board", b"ADY")
    assert consumer.origin_ns == 2 and consumer._ready_tails["main"] == b""


def test_ready_rejects_lower_bound_and_acceptance_notifications():
    from sim.api import Event
    from sim.apps.experiment import _Consumer

    consumer = _Consumer((), 10, {}, {"board.uart": "uart"},
                         ready=(ExperimentReady("main", "board.uart", "READY"),))
    consumer(Event(1, 0, "board", "uart", "input.delivered", b"READY", 0, 0, 0, "exact"))
    assert consumer.origin_ns is None
    with pytest.raises(ValueError, match="upper bound"):
        consumer(Event(1, 1, "board", "uart", "uart", b"READY", 700, 0, 0, "lower_bound"))
    assert consumer.origin_ns is None and consumer.ready_receipts == {}


def test_ready_and_acquisition_share_startup_deadline_and_keep_total_deadline(monkeypatch):
    import sim.apps.experiment as experiment
    import sim.runtime.simulation as runtime

    clock = {"now": 100.0}
    monkeypatch.setattr(experiment, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(runtime, "monotonic", lambda: clock["now"])
    deadlines = []

    class ClockPeripheral(Peripheral):
        def sample(self, cut):
            deadlines.append(self.context.deadline_monotonic)
            clock["now"] += .004
            super().sample(cut)

    class ClockFactory(PeripheralFactory):
        def create(self, spec, context):
            # Resource construction consumes the same startup budget.
            clock["now"] += .003
            instance = ClockPeripheral(spec, context, self)
            self.created.append(instance)
            return instance

    factory = ClockFactory()
    configured = Scene(Topology((NodeSpec("board", "fixture", {
        "uart_observations": ((10, b"READY"),)}),)), quantum_ns=10, duration_ns=10)
    result = run_experiment(configured, {"fixture": factory}, ready=(ExperimentReady("main", "board.uart", "READY"),),
                            startup_timeout_s=.012, host_timeout_s=.02)
    assert result.origin_ns == 10 and deadlines == [100.012, 100.012, 100.02]
    assert factory.closed == factory.created

    clock["now"] = 200.0
    factory = ClockFactory()
    absent = Scene(Topology((NodeSpec("board", "fixture"),)), quantum_ns=10, duration_ns=10)
    with pytest.raises(TimeoutError, match="deadline"):
        run_experiment(absent, {"fixture": factory}, ready=(ExperimentReady("main", "board.uart", "NEVER"),),
                       startup_timeout_s=.01, host_timeout_s=1)
    assert factory.closed == factory.created


def test_declared_experiment_runs_canonical_values_and_rejects_python_override():
    configured, registry, factory = scene()
    declared = Scene(configured.topology, duration_ns=30, quantum_ns=10,
        experiment=ExperimentSpec(inputs=(ExperimentInput(3, "board.uart", b"OK"),),
                                  assertions=(uart_assert("board.uart"),)))
    with pytest.raises(ValueError, match="overridden"):
        run_experiment(declared, registry, inputs=(ExperimentInput(1, "board.level", 1),))
    assert factory.created == []
    result = run_experiment(declared, registry)
    assert result.passed and result.origin_ns == 0 and result.ready_receipts == {}
    assert factory.created[0].calls == [(3, "uart", b"OK")]


def test_optional_factory_batch_admission_is_pure_and_rechecks_actual_origin():
    class BatchFactory(PeripheralFactory):
        def __init__(self):
            super().__init__()
            self.batches = []

        def validate_inputs(self, spec, entries, *, origin_ns=None):
            self.batches.append((origin_ns, entries))
            if len({(at, port) for at, port, value in entries}) < len(entries):
                raise ValueError("duplicate cut")

    configured, _, _ = scene(options={"uart_observations": ((10, b"READY"),)})
    factory = BatchFactory()
    inputs = (ExperimentInput(2, "board.level", 1), ExperimentInput(0, "board.uart", b"OK"))
    result = run_experiment(configured, {"fixture": factory}, ready=(ExperimentReady("main", "board.uart", "READY"),),
                            inputs=inputs)
    assert result.origin_ns == 10
    assert [origin for origin, _ in factory.batches] == [None, 0]
    assert factory.batches[0][1] == ((2, "level", 1), (0, "uart", b"OK"))
    assert factory.batches[1][1] == ((12, "level", 1), (10, "uart", b"OK"))
    factory = BatchFactory()
    with pytest.raises(ValueError, match="duplicate cut"):
        run_experiment(configured, {"fixture": factory}, inputs=(inputs[0], inputs[0]))
    assert factory.created == []


def test_yaml_host_can_input_uses_real_shared_bus_and_matches_delivery(tmp_path):
    import yaml
    from sim.composition import builtin_registry

    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump({"schema": "sim/v1",
        "runtime": {"quantum": "10ns", "duration": "20ns"},
        "nodes": {"host": {"kind": "device.can-host"},
                  "peer": {"kind": "device.can-host"},
                  "bus": {"kind": "bus.can", "delay_ns": 2}},
        "links": [["host.can", "bus.can"], ["peer.can", "bus.can"]],
        "experiment": {"inputs": [{"at": "3ns", "endpoint": "host.tx", "value": {
            "id": 0x321, "dlc": 2, "data_hex": "00ff"}}],
            "assertions": [{"before": "10ns", "can_seen": {
                "endpoint": "host.can", "id": 0x321, "flags": 0, "dlc": 2, "data_hex": "00ff"}}]}}))
    events = []
    result = run_experiment(path, builtin_registry(), on_event=events.append)
    assert result.passed and result.delivered_inputs == 1 and result.end_ns == 20
    receipt = result.matches[0]
    assert (receipt.kind, receipt.source_node, receipt.source_time_ns,
            receipt.mapped_time_ns, receipt.delivery_time_ns) == ("can.delivery", "host", 3, 3, 5)
    received = next(event for event in events if event.kind == "can-host.received")
    assert received.source_node == "peer"
    assert received.payload["frame"] == CanFrame(0x321, dlc=2, data=b"\0\xff", timestamp_ns=5)


def test_named_markers_unlock_individually_and_all_ready_keeps_session_assertions():
    factory = PeripheralFactory()
    configured = Scene(Topology((
        NodeSpec("west", "fixture", {"uart_observations": ((10, b"WEST startup"),)}),
        NodeSpec("east", "fixture", {"uart_observations": ((20, b"EAST"),)}),
    )), quantum_ns=10, duration_ns=20)
    markers = (ExperimentReady("west_ready", "west.uart", "WEST"),
               ExperimentReady("east_ready", "east.uart", "EAST"))
    result = run_experiment(configured, {"fixture": factory}, ready=markers,
        inputs=(ExperimentInput(0, "west.uart", b"marker", "marker:west_ready"),
                ExperimentInput(10, "west.uart", b"session", "session"),
                ExperimentInput(0, "east.uart", b"all-ready"),
                ExperimentInput(1, "east.uart", b"active")),
        assertions=({"origin": "session", **uart_assert("west.uart", "startup", "15ns")},
                    uart_assert("west.uart", "startup", "15ns"),
                    uart_assert("east.uart", "all-ready", "15ns"),
                    uart_assert("east.uart", "active", "15ns")))
    assert (result.origin_ns, result.end_ns, result.status) == (20, 40, ("pass", "fail", "fail", "pass"))
    assert list(result.ready_receipts) == ["west_ready", "east_ready"]
    assert [item.delivery_time_ns for item in result.ready_receipts.values()] == [10, 20]
    assert factory.created[0].calls == [(10, "uart", b"session"), (10, "uart", b"marker")]
    assert factory.created[1].calls == [(20, "uart", b"all-ready"), (21, "uart", b"active")]


def test_marker_input_can_make_peer_ready_before_any_additional_cpu_interval():
    configured, registry, factory = scene(("west", "east"), quantum=10, duration=5,
                                        options={"uart_observations": ((3, b"WEST"),)})
    result = run_experiment(configured, registry,
        ready=(ExperimentReady("west", "west.uart", "WEST"),
               ExperimentReady("east", "east.uart", "EAST")),
        inputs=(ExperimentInput(3, "west.level", 1, "session"),
                ExperimentInput(0, "east.uart", b"EAST", "marker:west")))
    assert result.origin_ns == 3 and result.end_ns == 8
    assert result.ready_receipts["west"].delivery_time_ns == result.ready_receipts["east"].delivery_time_ns == 3
    assert factory.created[1].calls == [(3, "uart", b"EAST")]


def test_session_input_at_zero_unlocks_ready_without_overshooting_short_active_duration():
    configured, registry, factory = scene(quantum=10, duration=3)
    result = run_experiment(configured, registry,
        ready=(ExperimentReady("boot", "board.uart", "READY"),),
        inputs=(ExperimentInput(0, "board.uart", b"READY", "session"),
                ExperimentInput(1, "board.uart", b"active")),
        assertions=({"origin": "session", **uart_assert("board.uart", "READY", "3ns")},))
    assert (result.origin_ns, result.end_ns, result.status) == (0, 3, ("pass",))
    assert factory.created[0].calls == [(0, "uart", b"READY"), (1, "uart", b"active")]


def test_same_event_unlocks_named_groups_in_input_declaration_order():
    configured, registry, factory = scene(quantum=10, duration=5,
        options={"uart_observations": ((0, b"AB"),)})
    result = run_experiment(configured, registry,
        ready=(ExperimentReady("a", "board.uart", "A"), ExperimentReady("b", "board.uart", "B")),
        inputs=(ExperimentInput(0, "board.uart", b"b", "marker:b"),
                ExperimentInput(0, "board.uart", b"a", "marker:a")))
    assert result.passed and factory.created[0].calls == [(0, "uart", b"b"), (0, "uart", b"a")]


@pytest.mark.parametrize("ready,inputs,assertions", [
    (None, (), ()),
    (ExperimentReady("a", "board.uart", "A"), (), ()),
    ((ExperimentReady("a", "board.uart", "A"),) * 2, (), ()),
    (tuple(ExperimentReady(f"m{index}", "board.uart", "A") for index in range(65)), (), ()),
    ((), (ExperimentInput(0, "board.uart", b"x", "marker:absent"),), ()),
    ((), (), ({"origin": "marker:a", **uart_assert("board.uart")},)),
])
def test_new_readiness_and_timeline_admission_has_no_resource_effects(ready, inputs, assertions):
    configured, registry, factory = scene()
    with pytest.raises((TypeError, ValueError)):
        run_experiment(configured, registry, ready=ready, inputs=inputs, assertions=assertions)
    assert factory.created == factory.closed == []


def test_source_cut_callback_uses_runtime_input_queue_before_commit_and_errors_cleanup():
    configured, registry, factory = scene(quantum=5, duration=10)
    cuts = []
    def inject(cut, simulation):
        cuts.append(cut)
        if cut == 5:
            simulation.schedule_input("board.uart", b"ACK", at_ns=cut)
    result = run_experiment(configured, registry, on_cut=inject,
                            assertions=(uart_assert("board.uart", "ACK", "10ns"),))
    assert result.status == ("pass",) and cuts == [0, 5, 10]
    assert factory.created[0].calls == [(5, "uart", b"ACK")]
    failure = RuntimeError("source cut observer failed")
    def bad(cut, simulation):
        if cut == 5:
            raise failure
    with pytest.raises(RuntimeError) as caught:
        run_experiment(configured, registry, on_cut=bad)
    assert caught.value is failure and factory.closed == factory.created
