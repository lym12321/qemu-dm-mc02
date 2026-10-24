"""Finite experiment consumers of actual public Simulation Events and cuts."""

from dataclasses import asdict
from pathlib import Path
import json

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.apps.experiment import ExperimentInput, run_experiment
from sim.config.scene import Scene
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
    assert consumer.evaluator.status == ("pending",) and consumer.matches == []


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
