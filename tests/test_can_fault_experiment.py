"""Direct YAML/Simulation consumers of the sole CAN fault policy owner."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from sim.api import NodeDescription, PortSpec, StepResult
from sim.apps.experiment import run_experiment
from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.fabric import CanBusFactory, CanFrame
from sim.runtime import Simulation, SimulationState


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/can-faults/host-only.yaml"
SELECTIVE = {"drop": [{"source": "host.can", "destination": "peer.can", "can_id": 0x321}]}


def scene_path(tmp_path, *, bus_options=None, second_bus=False, machine=False):
    nodes = {"host": {"kind": "test.machine" if machine else "device.can-host"},
             "peer": {"kind": "device.can-host"}, "tap": {"kind": "device.can-host"},
             "can0": {"kind": "bus.can", "delay_ns": 2,
                      "fault_profiles": {"selective": SELECTIVE}, **(bus_options or {})}}
    links = [[f"{name}.can", "can0.can"] for name in ("host", "peer", "tap")]
    if second_bus:
        nodes.update({"other": {"kind": "device.can-host"},
                      "sink": {"kind": "device.can-host"}, "can1": {"kind": "bus.can", "delay_ns": 2}})
        links.extend([["other.can", "can1.can"], ["sink.can", "can1.can"]])
    path = tmp_path / "scene.yaml"
    path.write_text(yaml.safe_dump({"schema": "sim/v1", "nodes": nodes, "links": links,
        "runtime": {"quantum": "10ns", "duration": "30ns"}}, sort_keys=False))
    return path


def simulation(path, registry=None, **options):
    registry = builtin_registry() if registry is None else registry
    scene = load_scene(path, registry)
    return Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns, **options)


def clocks(event):
    return event.source_time_ns, event.mapped_time_ns, event.delivery_time_ns


def deliveries(events):
    return [(f"{event.source_node}.{event.source_port}", event.payload["destination"],
             event.payload["frame"], event.payload["bus_sequence"], event.payload["due_ns"],
             clocks(event), event.payload["dropped"], event.payload["late"])
            for event in events if event.kind == "can.delivery"]


def received(events):
    return [(event.source_node, event.payload["frame"], clocks(event))
            for event in events if event.kind == "can-host.received"]


def track_owners(monkeypatch, registry, *, cleanup_error=None):
    """Observe real owners and cleanup; keep their actual queue/dispatch intact."""
    created, closed = [], []
    for kind in ("bus.can", "device.can-host"):
        factory = registry[kind]
        create = factory.create

        def traced_create(spec, context, create=create):
            owner = create(spec, context)
            created.append((spec.name, owner))
            close = owner.close

            def traced_close():
                closed.append((spec.name, owner))
                close()
                if cleanup_error is not None and spec.name == "can0":
                    raise cleanup_error

            monkeypatch.setattr(owner, "close", traced_close)
            return owner

        monkeypatch.setattr(factory, "create", traced_create)
    return created, closed


def test_host_only_example_receipts_include_each_recipient_and_pending_policy(monkeypatch):
    registry = builtin_registry()
    created, closed = track_owners(monkeypatch, registry)
    scene = load_scene(EXAMPLE, registry)
    events = []
    result = run_experiment(scene, registry, on_event=events.append)
    assert result.passed and result.status == ("pass",) * 5
    assert (result.delivered_inputs, result.origin_ns, result.end_ns) == (9, 0, 30)
    # Literal source frames retain enqueue timestamps; delivery clocks and
    # received frames separately expose the actual completed delivery cuts.
    assert deliveries(events) == [
        ("host.can", "peer.can", CanFrame(0x321, data=b"\xaa", timestamp_ns=1), 0, 3, (1, 1, 3), False, False),
        ("host.can", "tap.can", CanFrame(0x321, data=b"\xaa", timestamp_ns=1), 0, 3, (1, 1, 3), False, False),
        ("host.can", "peer.can", CanFrame(0x321, data=b"\xcc", timestamp_ns=8), 2, 10, (8, 8, 10), True, False),
        ("host.can", "tap.can", CanFrame(0x321, data=b"\xcc", timestamp_ns=8), 2, 10, (8, 8, 10), False, False),
        ("tap.can", "host.can", CanFrame(0x321, data=b"\xdd", timestamp_ns=9), 3, 11, (9, 9, 11), False, False),
        ("tap.can", "peer.can", CanFrame(0x321, data=b"\xdd", timestamp_ns=9), 3, 11, (9, 9, 11), False, False),
        ("host.can", "peer.can", CanFrame(0x322, data=b"\xee", timestamp_ns=10), 4, 12, (10, 10, 12), False, False),
        ("host.can", "tap.can", CanFrame(0x322, data=b"\xee", timestamp_ns=10), 4, 12, (10, 10, 12), False, False),
        ("host.can", "peer.can", CanFrame(0x321, data=b"\xbb", timestamp_ns=5), 1, 17, (5, 5, 17), True, False),
        ("host.can", "tap.can", CanFrame(0x321, data=b"\xbb", timestamp_ns=5), 1, 17, (5, 5, 17), False, False),
        ("host.can", "peer.can", CanFrame(0x321, data=b"\xff", timestamp_ns=19), 5, 21, (19, 19, 21), False, False),
        ("host.can", "tap.can", CanFrame(0x321, data=b"\xff", timestamp_ns=19), 5, 21, (19, 19, 21), False, False),
    ]
    assert received(events) == [
        ("peer", CanFrame(0x321, data=b"\xaa", timestamp_ns=3), (3, 3, 3)),
        ("tap", CanFrame(0x321, data=b"\xaa", timestamp_ns=3), (3, 3, 3)),
        ("tap", CanFrame(0x321, data=b"\xcc", timestamp_ns=10), (10, 10, 10)),
        ("host", CanFrame(0x321, data=b"\xdd", timestamp_ns=11), (11, 11, 11)),
        ("peer", CanFrame(0x321, data=b"\xdd", timestamp_ns=11), (11, 11, 11)),
        ("peer", CanFrame(0x322, data=b"\xee", timestamp_ns=12), (12, 12, 12)),
        ("tap", CanFrame(0x322, data=b"\xee", timestamp_ns=12), (12, 12, 12)),
        ("tap", CanFrame(0x321, data=b"\xbb", timestamp_ns=17), (17, 17, 17)),
        ("peer", CanFrame(0x321, data=b"\xff", timestamp_ns=21), (21, 21, 21)),
        ("tap", CanFrame(0x321, data=b"\xff", timestamp_ns=21), (21, 21, 21)),
    ]
    controls = [event for event in events if event.kind == "can.fault.applied"]
    assert [(event.source_node, event.source_port, dict(event.payload), clocks(event)) for event in controls] == [
        ("can0", "fault", {"previous_profile": "normal", "profile": "delayed", "delay_ns": 12,
                           "drop": (), "cut_ns": 4}, (4, 4, 4)),
        ("can0", "fault", {"previous_profile": "delayed", "profile": "selective", "delay_ns": 2,
                           "drop": ({"source": "host.can", "destination": "peer.can", "can_id": 0x321},),
                           "cut_ns": 7}, (7, 7, 7)),
        ("can0", "fault", {"previous_profile": "selective", "profile": "normal", "delay_ns": 2,
                           "drop": (), "cut_ns": 18}, (18, 18, 18)),
    ]
    assert [event.sequence for event in events] == list(range(result.events_total))
    assert all(event.quality == "exact" for event in events)
    assert closed == list(reversed(created))
    assert next(owner for name, owner in created if name == "can0").pending == 0


def test_host_only_scene_is_runnable_from_public_cli(tmp_path):
    report = tmp_path / "report.json"
    for arguments in (("validate", str(EXAMPLE)), ("run", str(EXAMPLE), "--report", str(report))):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "tools/sim"), *arguments],
                                cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    payload = json.loads(report.read_text())
    assert payload["status"] == "passed"
    assert payload["result"]["status"] == ["pass"] * 5
    assert (payload["result"]["delivered_inputs"], payload["result"]["end_ns"]) == (9, 30)


def test_rule_list_or_and_destination_fields_preserve_other_traffic(tmp_path):
    path = scene_path(tmp_path, bus_options={"fault_profiles": {"mixed": {"drop": [
        {"source": "host.can"}, {"destination": "peer.can", "can_id": 0x333}]}}})
    with simulation(path) as sim:
        sim.schedule_input("can0.fault", "mixed", at_ns=1)
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"a"), at_ns=2)
        sim.schedule_input("tap.tx", CanFrame(0x333, data=b"b"), at_ns=3)
        sim.schedule_input("peer.tx", CanFrame(0x444, data=b"c"), at_ns=4)
        sim.run_until(20)
        assert [(row[0], row[1], row[2].can_id, row[5], row[6]) for row in deliveries(sim.events)] == [
            ("host.can", "peer.can", 0x321, (2, 2, 4), True),
            ("host.can", "tap.can", 0x321, (2, 2, 4), True),
            ("tap.can", "host.can", 0x333, (3, 3, 5), False),
            ("tap.can", "peer.can", 0x333, (3, 3, 5), True),
            ("peer.can", "host.can", 0x444, (4, 4, 6), False),
            ("peer.can", "tap.can", 0x444, (4, 4, 6), False)]
        assert received(sim.events) == [
            ("host", CanFrame(0x333, data=b"b", timestamp_ns=5), (5, 5, 5)),
            ("host", CanFrame(0x444, data=b"c", timestamp_ns=6), (6, 6, 6)),
            ("tap", CanFrame(0x444, data=b"c", timestamp_ns=6), (6, 6, 6))]


@pytest.mark.parametrize("control_first,due", [(True, 10), (False, 5)])
def test_same_cut_input_fifo_controls_only_subsequent_enqueue_delay(tmp_path, control_first, due):
    path = scene_path(tmp_path, bus_options={"fault_profiles": {"delayed": {"delay_ns": 7}}})
    inputs = [("can0.fault", "delayed"), ("host.tx", CanFrame(0x321, data=b"x"))]
    with simulation(path) as sim:
        for endpoint, value in inputs if control_first else reversed(inputs):
            sim.schedule_input(endpoint, value, at_ns=3)
        sim.run_until(20)
        assert [(event.source_node, event.source_port) for event in sim.events
                if event.kind == "input.delivered"] == (
                    [("can0", "fault"), ("host", "tx")] if control_first else
                    [("host", "tx"), ("can0", "fault")])
        assert deliveries(sim.events) == [
            ("host.can", "peer.can", CanFrame(0x321, data=b"x", timestamp_ns=3),
             0, due, (3, 3, due), False, False),
            ("host.can", "tap.can", CanFrame(0x321, data=b"x", timestamp_ns=3),
             0, due, (3, 3, due), False, False)]


def test_same_cut_inputs_finish_before_dispatch_and_last_profile_selects_drop(tmp_path):
    path = scene_path(tmp_path, bus_options={"delay_ns": 0})
    with simulation(path) as sim:
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"a"), at_ns=3)
        sim.schedule_input("can0.fault", "selective", at_ns=3)
        sim.schedule_input("can0.fault", "normal", at_ns=3)
        sim.schedule_input("can0.fault", "selective", at_ns=3)
        sim.run_until(20)
        assert deliveries(sim.events) == [
            ("host.can", "peer.can", CanFrame(0x321, data=b"a", timestamp_ns=3), 0, 3, (3, 3, 3), True, False),
            ("host.can", "tap.can", CanFrame(0x321, data=b"a", timestamp_ns=3), 0, 3, (3, 3, 3), False, False)]
        assert received(sim.events) == [("tap", CanFrame(0x321, data=b"a", timestamp_ns=3), (3, 3, 3))]
        inputs = [event.sequence for event in sim.events if event.kind == "input.delivered"]
        assert max(inputs) < next(event.sequence for event in sim.events if event.kind == "can.delivery")


def test_machine_completion_already_queued_output_keeps_due_but_uses_new_drop(tmp_path):
    class Machine:
        def __init__(self, context):
            self.connection = context.attach("can", self)

        def prepare_step(self, context):
            pass

        def request_step(self, context):
            pass

        def finish_step(self, context):
            if context.t_sim_ns == 3:
                self.connection.send(CanFrame(0x321, data=b"m", timestamp_ns=3))
            return StepResult("ok", context.t_sim_ns)

        def receive(self, frame):
            pytest.fail("the fixture emits one frame and receives no traffic")

        def close(self):
            pass

    class MachineFactory:
        def describe(self, spec):
            return NodeDescription((PortSpec("can", "can", min_connections=1),), {"machine"})

        def create(self, spec, context):
            return Machine(context)

    registry = builtin_registry()
    registry["test.machine"] = MachineFactory()
    path = scene_path(tmp_path, machine=True, bus_options={"fault_profiles": {
        "selected": {"delay_ns": 9, **SELECTIVE}}})
    with simulation(path, registry) as sim:
        sim.schedule_input("can0.fault", "selected", at_ns=3)
        sim.run_until(20)
        assert deliveries(sim.events) == [
            ("host.can", "peer.can", CanFrame(0x321, data=b"m", timestamp_ns=3), 0, 5, (3, 3, 5), True, False),
            ("host.can", "tap.can", CanFrame(0x321, data=b"m", timestamp_ns=3), 0, 5, (3, 3, 5), False, False)]
        assert received(sim.events) == [("tap", CanFrame(0x321, data=b"m", timestamp_ns=5), (5, 5, 5))]
        sent = next(event for event in sim.events if event.kind == "can.sent")
        applied = next(event for event in sim.events if event.kind == "can.fault.applied")
        assert sent.sequence < applied.sequence


def test_second_bus_keeps_base_delay_and_delivery_during_first_bus_fault(tmp_path):
    path = scene_path(tmp_path, second_bus=True, bus_options={"fault_profiles": {
        "selected": {"delay_ns": 9, **SELECTIVE}}})
    with simulation(path) as sim:
        sim.schedule_input("can0.fault", "selected", at_ns=1)
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"a"), at_ns=3)
        sim.schedule_input("other.tx", CanFrame(0x321, data=b"b"), at_ns=3)
        sim.run_until(20)
        assert deliveries(sim.events) == [
            ("other.can", "sink.can", CanFrame(0x321, data=b"b", timestamp_ns=3), 0, 5, (3, 3, 5), False, False),
            ("host.can", "peer.can", CanFrame(0x321, data=b"a", timestamp_ns=3), 0, 12, (3, 3, 12), True, False),
            ("host.can", "tap.can", CanFrame(0x321, data=b"a", timestamp_ns=3), 0, 12, (3, 3, 12), False, False)]
        assert received(sim.events) == [
            ("sink", CanFrame(0x321, data=b"b", timestamp_ns=5), (5, 5, 5)),
            ("tap", CanFrame(0x321, data=b"a", timestamp_ns=12), (12, 12, 12))]


def test_healthy_reset_discards_pending_frame_and_restores_normal_base_policy(tmp_path, monkeypatch):
    registry = builtin_registry()
    created, closed = track_owners(monkeypatch, registry)
    path = scene_path(tmp_path, bus_options={"fault_profiles": {
        "selected": {"delay_ns": 9, **SELECTIVE}}})
    with simulation(path, registry) as sim:
        sim.schedule_input("can0.fault", "selected", at_ns=1)
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"old"), at_ns=3)
        sim.run_until(3)
        previous = sim.components["can0"]
        assert previous.pending == 1
        sim.reset()
        assert previous.pending == 0 and len(closed) == 4
        assert sim.session.session_id == 2 and sim.t_sim_ns == 0
        assert sim.components["can0"].bus.delay_ns == 2
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"new"), at_ns=1)
        sim.run_until(20)
        current = [event for event in sim.events if event.session_id == 2]
        assert deliveries(current) == [
            ("host.can", "peer.can", CanFrame(0x321, data=b"new", timestamp_ns=1), 0, 3, (1, 1, 3), False, False),
            ("host.can", "tap.can", CanFrame(0x321, data=b"new", timestamp_ns=1), 0, 3, (1, 1, 3), False, False)]
        assert [frame.data for _, frame, _ in received(current)] == [b"new", b"new"]
        assert not any(event.kind == "can.fault.applied" for event in current)
    assert len(closed) == len(created) == 8


def assert_frozen_without_replay(sim, primary):
    assert sim.state == SimulationState.FAILED and sim.failure is primary
    count = sim.events_total
    with pytest.raises(RuntimeError, match="healthy"):
        sim.run_until(30)
    with pytest.raises(RuntimeError, match="healthy"):
        sim.reset()
    assert sim.failure is primary and sim.events_total == count


def test_fault_profile_capacity_failure_retains_pending_and_closes_without_replay(tmp_path, monkeypatch):
    registry = builtin_registry()
    created, closed = track_owners(monkeypatch, registry)
    path = scene_path(tmp_path, bus_options={"max_pending": 1,
        "fault_profiles": {"delayed": {"delay_ns": 9}}})
    sim = simulation(path, registry)
    with sim:
        network, host = sim.components["can0"], sim.components["host"]
        sim.schedule_input("can0.fault", "delayed", at_ns=3)
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"first"), at_ns=3)
        sim.schedule_input("host.tx", CanFrame(0x321, data=b"second"), at_ns=3)
        with pytest.raises(BufferError, match="capacity") as error:
            sim.run_until(20)
        assert_frozen_without_replay(sim, error.value)
        assert network.pending == 1 and network.bus.delay_ns == 9
        assert deliveries(sim.events) == [] and received(sim.events) == []
    assert network.pending == 0 and closed == list(reversed(created))
    with pytest.raises(RuntimeError, match="closed"):
        host.input("tx", CanFrame(0x321))


@pytest.mark.parametrize("failure_surface", ["control-observer", "delivery-callback"])
def test_observer_failure_freezes_partial_effects_and_preserves_primary_on_cleanup(
        tmp_path, monkeypatch, failure_surface):
    primary = OSError(failure_surface)
    secondary = OSError("CAN cleanup secondary")

    def on_event(event):
        if event.kind == "can.fault.applied":
            raise primary

    def on_delivery(delivery):
        raise primary

    registry = builtin_registry()
    if failure_surface == "delivery-callback":
        registry["bus.can"] = CanBusFactory(on_delivery=on_delivery)
    created, closed = track_owners(monkeypatch, registry, cleanup_error=secondary)
    path = scene_path(tmp_path)
    sim = simulation(path, registry, on_event=on_event if failure_surface == "control-observer" else None)
    sim.start()
    network, host = sim.components["can0"], sim.components["host"]
    sim.schedule_input("host.tx", CanFrame(0x321, data=b"first"), at_ns=3)
    if failure_surface == "control-observer":
        sim.schedule_input("can0.fault", "selective", at_ns=4)
    else:
        sim.schedule_input("host.tx", CanFrame(0x322, data=b"later"), at_ns=4)
    with pytest.raises(OSError) as error:
        sim.run_until(20)
    assert error.value is primary
    assert_frozen_without_replay(sim, primary)
    assert network.pending == 1 and received(sim.events) == []
    if failure_surface == "control-observer":
        assert deliveries(sim.events) == []
        assert [event.payload["profile"] for event in sim.events if event.kind == "can.fault.applied"] == ["selective"]
        assert sim.t_sim_ns == 3
    else:
        assert deliveries(sim.events) == [
            ("host.can", "peer.can", CanFrame(0x321, data=b"first", timestamp_ns=3),
             0, 5, (3, 3, 5), False, False)]
        assert sim.t_sim_ns == 4
    with pytest.raises(OSError) as cleanup:
        sim.close()
    assert cleanup.value is primary
    assert any("CAN cleanup secondary" in note for note in primary.__notes__)
    assert network.pending == 0 and closed == list(reversed(created))
    assert sim.state == SimulationState.CLOSED
    with pytest.raises(RuntimeError, match="closed"):
        host.input("tx", CanFrame(0x321))
    count = sim.events_total
    sim.close()
    assert sim.events_total == count and len(closed) == 4
