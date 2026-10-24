"""External admission errors stay reversible; execution errors remain terminal."""

import pytest

from sim.api import NodeDescription, NodeSpec, PortSpec, Topology
from sim.runtime import Simulation, SimulationState


class Values:
    def __init__(self):
        self.applied = []
        self.closed = False

    def input(self, port, value):
        self.applied.append((port, value))

    def close(self):
        self.closed = True


class ValuesFactory:
    def describe(self, spec):
        return NodeDescription((
            PortSpec("value", "level", "in", capabilities={"external_input"}),
            PortSpec("internal", "level", "in"),
            PortSpec("output", "level", "out")), {"input"})

    def validate_input(self, spec, port, value, *, connected_ports):
        if type(value) is not int:
            raise TypeError("level must be an integer")
        if value not in (0, 1):
            raise ValueError("level must be integer 0 or 1")
        return value

    def create(self, spec, context):
        self.instance = Values()
        return self.instance


def simulation(**kwargs):
    factory = ValuesFactory()
    return Simulation(Topology((NodeSpec("n", "values"),)), {"values": factory}, **kwargs), factory


@pytest.mark.parametrize("endpoint,value", [
    ("missing.value", 0), ("n.missing", 0), ("n.internal", 0),
    ("n.output", 0), ("n.value", True), ("n.value", "1"), ("n.value", 2),
])
def test_invalid_admission_does_not_enqueue_apply_or_freeze(endpoint, value):
    sim, factory = simulation()
    with sim:
        before = sim.events_total
        with pytest.raises((ValueError, TypeError)):
            sim.schedule_input(endpoint, value)
        assert sim.state == SimulationState.PAUSED
        assert sim.events_total == before and factory.instance.applied == []
        sim.schedule_input("n.value", 1)
        sim.run_until(0)
        assert factory.instance.applied == [("value", 1)]
        delivered = [e for e in sim.events if e.kind == "input.delivered"]
        assert len(delivered) == 1
    assert factory.instance.closed


def test_interactive_recovers_from_payload_and_capability_errors(tmp_path, monkeypatch, capsys):
    import sys
    from sim.cli import _interactive
    source = tmp_path / "commands"
    source.write_text("inject n.value true\ninject n.internal 1\ninject n.value 1\n"
                      "step 1ns\ninject n.value 0\nstep 1ns\nquit\n")
    sim, factory = simulation(max_pending_inputs=1)
    with sim, source.open() as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        _interactive(sim, None)
        assert sim.state == SimulationState.PAUSED
        assert factory.instance.applied == [("value", 1), ("value", 0)]
    error = capsys.readouterr().err
    assert "integer" in error and "external input" in error


def test_interactive_capacity_failure_escapes_before_any_input_is_applied(tmp_path, monkeypatch):
    import sys
    from sim.cli import _interactive
    source = tmp_path / "commands"
    source.write_text("inject n.value 1\ninject n.value 0\nstep 1ns\n")
    sim, factory = simulation(max_pending_inputs=1)
    with sim, source.open() as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        with pytest.raises(BufferError, match="input capacity"):
            _interactive(sim, None)
        assert sim.state == SimulationState.FAILED
        assert factory.instance.applied == []
    assert factory.instance.closed


def test_repeated_inputs_keep_order_without_reapplying_at_same_cut():
    sim, factory = simulation(max_pending_inputs=2, quantum_ns=10)
    with sim:
        sim.schedule_input("n.value", 1, at_ns=3)
        sim.schedule_input("n.value", 1, at_ns=3)
        assert sim.state == SimulationState.PAUSED
        sim.step(10)
        assert factory.instance.applied == [("value", 1), ("value", 1)]
        assert [e.delivery_time_ns for e in sim.events if e.kind == "input.delivered"] == [3, 3]
        with pytest.raises(ValueError, match="precede"):
            sim.schedule_input("n.value", 0, at_ns=9)
        sim.run_until(10)
        assert factory.instance.applied == [("value", 1), ("value", 1)]


def test_device_failure_after_admission_freezes_without_replay_and_closes():
    sim, factory = simulation()
    with sim:
        def fail(port, value):
            factory.instance.applied.append((port, value))
            raise OSError("device effect failed")
        factory.instance.input = fail
        sim.schedule_input("n.value", 1)
        with pytest.raises(OSError, match="device effect"):
            sim.step()
        assert sim.state == SimulationState.FAILED
        with pytest.raises(RuntimeError):
            sim.step()
        assert factory.instance.applied == [("value", 1)]
    assert factory.instance.closed


@pytest.mark.parametrize('surface', ['event', 'component'])
@pytest.mark.parametrize('replacement', [False, True])
def test_caught_capacity_failure_still_stops_dispatch_and_preserves_primary(surface, replacement):
    caught_errors = []
    def exhaust():
        sim.schedule_input('n.value', 0, at_ns=5)
        try:
            sim.schedule_input('n.value', 1, at_ns=5)
        except BufferError as error:
            caught_errors.append(error)
            if replacement:
                raise OSError('replacement input failure') from error
    def observe(event):
        if surface == 'event' and event.kind == 'input.delivered':
            exhaust()
    sim, factory = simulation(max_pending_inputs=1, quantum_ns=10, on_event=observe)
    with sim:
        owner = factory.instance
        if surface == 'component':
            original = owner.input
            def consume(port, value):
                original(port, value)
                exhaust()
            owner.input = consume
        sim.schedule_input('n.value', 1, at_ns=3)
        with pytest.raises(BufferError) as raised:
            sim.run_until(20)
        assert raised.value is sim.failure is caught_errors[0]
        if replacement:
            assert any('OSError: replacement input failure' in note
                       for note in sim.failure.__notes__)
        assert sim.state == 'failed' and sim.t_sim_ns == sim.step_id == 0
        assert owner.applied == [('value', 1)]
        assert len(sim.events) == (1 if surface == 'event' else 0)
