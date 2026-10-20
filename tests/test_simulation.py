"""Sampled causality, resource ownership and failure gates of the public runtime."""

from dataclasses import dataclass

import pytest

from sim.api import Event, LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.fabric import CanBus, CanBusFactory, CanFrame
from sim.runtime import Simulation, SimulationState
import sim.runtime.simulation as runtime


class Factory:
    def __init__(self, build, capabilities=(), ports=()):
        self.build = build
        self.description = NodeDescription(ports, frozenset(capabilities))
        self.instances = []

    def describe(self, spec):
        return self.description

    def create(self, spec, context):
        result = self.build(spec, context)
        self.instances.append(result)
        return result


class Component:
    def __init__(self, spec, context, trace=None):
        self.name = spec.name
        self.context = context
        self.trace = trace if trace is not None else []
        self.close_count = 0
        self.sessions = []

    def reset(self, session):
        self.sessions.append(session.session_id)

    def close(self):
        self.close_count += 1
        self.trace.append(("close", self.name))


class Machine(Component):
    def __init__(self, spec, context, trace, *, send=False):
        super().__init__(spec, context, trace)
        self.frames = []
        self.input_values = []
        self.request_views = []
        self.sender = send
        self.connection = context.attach_can("can", self)

    def receive(self, frame):
        self.frames.append(frame)

    def input(self, port, payload):
        self.input_values.append((port, payload))

    def request_step(self, ctx):
        self.trace.append(("request", self.name))
        self.request_views.append(tuple(self.frames))

    def finish_step(self, ctx):
        self.trace.append(("finish", self.name))
        if self.sender and ctx.step_id == 1:
            self.connection.send(CanFrame(0x123, data=b"\x07", timestamp_ns=2))
        self.context.emit(None, "machine.completed", {"target": ctx.t_sim_ns})
        return StepResult("ok", ctx.t_sim_ns)


class Plant(Component):
    def __init__(self, spec, context, trace):
        super().__init__(spec, context, trace)
        self.control = 0
        self.integrated_controls = []
        self.connection = context.attach_can("can", self)

    def receive(self, frame):
        self.control = frame.data[0]

    def step(self, ctx):
        self.integrated_controls.append(self.control)
        self.context.emit("can", "plant.completed", {"control": self.control})
        return StepResult("ok", ctx.t_sim_ns)


def scene(*, delay=0, on_event=None, mode="stepped"):
    trace = []
    machine = Factory(lambda spec, ctx: Machine(spec, ctx, trace, send=spec.name == "first"),
                      {"machine", "input"}, (PortSpec("can", "can"), PortSpec("key", "digital", "in", "signal")))
    plant = Factory(lambda spec, ctx: Plant(spec, ctx, trace), {"step"}, (PortSpec("can", "can"),))
    topology = Topology((NodeSpec("first", "machine"), NodeSpec("bus", "bus", {"delay_ns": delay}),
                         NodeSpec("plant", "plant"), NodeSpec("second", "machine")),
                        tuple(LinkSpec(f"{name}.can", "bus.can") for name in ("first", "second", "plant")))
    simulation = Simulation(topology, {"bus": CanBusFactory(), "machine": machine, "plant": plant},
                            quantum_ns=10, on_event=on_event, mode=mode)
    return simulation, machine, plant, trace


class InputPlant(Component):
    """Record the control actually held over each completed interval."""

    def __init__(self, spec, context):
        super().__init__(spec, context)
        self.control = 0
        self.intervals = []
        self.inputs = []

    def input(self, port, payload):
        self.control = payload
        self.inputs.append((port, payload))

    def step(self, ctx):
        self.intervals.append((ctx.step_id, ctx.t_sim_ns - ctx.dt_ns,
                               ctx.t_sim_ns, self.control))
        return StepResult("ok", ctx.t_sim_ns)


def input_scene(**kwargs):
    factory = Factory(InputPlant, {"step", "input"},
                      (PortSpec("key", "digital", "in", "signal"),))
    simulation = Simulation(Topology((NodeSpec("plant", "plant"),)),
                            {"plant": factory}, quantum_ns=10, **kwargs)
    return simulation, factory


@pytest.mark.parametrize("operation", ["step", "run_until"])
def test_known_off_grid_pulse_cuts_intervals_and_quantum_is_a_maximum(operation):
    simulation, factory = input_scene()
    with simulation:
        simulation.schedule_input("plant.key", 1, at_ns=3)
        simulation.schedule_input("plant.key", 0, at_ns=7)
        result = getattr(simulation, operation)(22)
        assert factory.instances[0].intervals == [
            (1, 0, 3, 0), (2, 3, 7, 1), (3, 7, 17, 0), (4, 17, 22, 0)]
        assert [(event.source_time_ns, event.delivery_time_ns)
                for event in simulation.events if event.kind == "input.delivered"] == [(3, 3), (7, 7)]
        assert result.status == "ok" and result.reached_time_ns == 22
        assert result.diagnostics["requested_time_ns"] == 22
        assert result.diagnostics["substeps_completed"] == 4
        assert result.diagnostics["step_id"] == 4
        # Aggregation keeps only the final component result, not an interval log.
        assert result.diagnostics["components"] == {"plant": StepResult("ok", 22)}


def test_run_until_current_settles_due_inputs_without_new_intervals_or_step_ids():
    simulation, factory = input_scene()
    with simulation:
        simulation.schedule_input("plant.key", 1)
        simulation.schedule_input("plant.key", 2)
        result = simulation.run_until(0)
        assert factory.instances[0].inputs == [("key", 1), ("key", 2)]
        assert factory.instances[0].intervals == []
        assert (simulation.t_sim_ns, simulation.step_id) == (0, 0)
        assert result.diagnostics["substeps_completed"] == 0
        assert result.diagnostics["components"] == {}


def test_callbacks_can_add_the_next_future_cut_after_current_delivery():
    simulation, factory = input_scene()
    with simulation:
        simulation.schedule_input("plant.key", 1, at_ns=3)
        def schedule_release(event):
            if event.kind == "input.delivered" and event.payload == 1:
                simulation.schedule_input("plant.key", 0, at_ns=7)
        simulation.on_event = schedule_release
        simulation.step(10)
        assert factory.instances[0].intervals == [(1, 0, 3, 0), (2, 3, 7, 1), (3, 7, 10, 0)]


def test_same_cut_inputs_keep_enqueue_order_before_due_can_delivery():
    simulation, machines, plants, _ = scene()
    with simulation:
        machines.instances[0].connection.send(CanFrame(1, data=b"\x03"))
        simulation.schedule_input("first.key", 1)
        simulation.schedule_input("first.key", 2)
        result = simulation.run_until(0)
        kinds = [event.kind for event in simulation.events]
        assert kinds == ["can.sent", "input.delivered", "input.delivered", "can.delivery", "can.delivery"]
        assert machines.instances[0].input_values == [("key", 1), ("key", 2)]
        assert plants.instances[0].control == 3
        assert plants.instances[0].integrated_controls == []
        assert result.reached_time_ns == simulation.step_id == 0


@pytest.mark.parametrize("operation", ["step", "run_until"])
def test_pause_returns_partial_result_at_committed_internal_cut_and_resume_clears_request(operation):
    simulation, factory = input_scene()
    with simulation:
        simulation.schedule_input("plant.key", 1, at_ns=3)
        simulation.on_event = lambda event: simulation.pause()
        result = getattr(simulation, operation)(10)
        assert (result.status, result.reached_time_ns) == ("paused", 3)
        assert result.diagnostics["requested_time_ns"] == 10
        assert result.diagnostics["substeps_completed"] == 1
        assert factory.instances[0].intervals == [(1, 0, 3, 0)]
        simulation.on_event = None
        assert simulation.run_until(10).status == "ok"
        assert factory.instances[0].intervals[-1] == (2, 3, 10, 1)


def test_second_internal_interval_failure_retains_first_commit_and_freezes():
    simulation, factory = input_scene()
    failure = OSError("second interval failed")
    with simulation:
        simulation.schedule_input("plant.key", 1, at_ns=3)
        def fail_second(ctx):
            if ctx.step_id == 2:
                raise failure
            return StepResult("ok", ctx.t_sim_ns)
        factory.instances[0].step = fail_second
        with pytest.raises(OSError) as caught:
            simulation.step(10)
        assert caught.value is failure
        assert (simulation.t_sim_ns, simulation.step_id) == (3, 1)
        assert simulation.state == SimulationState.FAILED
        with pytest.raises(RuntimeError):
            simulation.run_until(10)
    assert factory.instances[0].close_count == 1


def test_global_can_budget_is_enforced_before_additional_broadcast_callbacks():
    simulation, machines, _, _ = scene()
    simulation.dispatch_budget = 1
    with simulation:
        first, second = machines.instances
        first.connection.send(CanFrame(1, data=b"\x01"))
        first.connection.send(CanFrame(2, data=b"\x02"))
        with pytest.raises(RuntimeError, match="dispatch budget"):
            simulation.run_until(1)
        assert [frame.can_id for frame in second.frames] == [1]
        assert simulation.components["bus"].pending == 1
        assert simulation.state == SimulationState.FAILED


def test_can_budget_does_not_refresh_when_callbacks_schedule_more_current_inputs():
    class Alternating(Component):
        def __init__(self, spec, context):
            super().__init__(spec, context)
            self.link = context.attach_can("can", self)
            self.inputs = self.received = 0

        def input(self, port, payload):
            self.inputs += 1
            self.link.send(CanFrame(1))

        def receive(self, frame):
            self.received += 1
            simulation.schedule_input("n.key", 1)

    factory = Factory(Alternating, {"input"},
                      (PortSpec("can", "can"), PortSpec("key", "digital", "in", "signal")))
    topology = Topology((NodeSpec("bus", "bus", {"loopback": True, "dispatch_budget": 1}),
                         NodeSpec("n", "n")), (LinkSpec("n.can", "bus.can"),))
    simulation = Simulation(topology, {"bus": CanBusFactory(), "n": factory}, dispatch_budget=5)
    with simulation:
        simulation.schedule_input("n.key", 1)
        with pytest.raises(RuntimeError, match="dispatch budget"):
            simulation.run_until(0)
        assert (factory.instances[0].inputs, factory.instances[0].received) == (2, 1)
        assert simulation.components["bus"].pending == 1


@pytest.mark.parametrize("mode", ["stepped", "interactive"])
def test_two_machines_share_bus_all_requests_precede_completions_and_no_retroactive_plant_control(mode):
    all_events = []
    simulation, machines, plants, trace = scene(on_event=all_events.append, mode=mode)
    with simulation:
        first, second = machines.instances
        plant = plants.instances[0]
        simulation.step()
        assert trace[:4] == [("request", "first"), ("request", "second"),
                             ("finish", "first"), ("finish", "second")]
        assert plant.integrated_controls == [0]
        assert plant.control == 7
        assert second.request_views == [()]
        assert second.frames == [CanFrame(0x123, data=b"\x07", timestamp_ns=10)]
        simulation.step()
        assert plant.integrated_controls == [0, 7]
        assert second.request_views[-1] == tuple(second.frames)
        sent = next(event for event in all_events if event.kind == "can.sent")
        deliveries = [event for event in all_events if event.kind == "can.delivery"]
        assert (sent.source_node, sent.source_port, sent.source_time_ns, sent.mapped_time_ns) == ("first", "can", 2, 2)
        assert [(event.source_time_ns, event.delivery_time_ns, event.payload["due_ns"])
                for event in deliveries] == [(2, 10, 10), (2, 10, 10)]
        assert simulation.t_sim_ns == 20
    assert first.close_count == second.close_count == plant.close_count == 1


def test_interval_floor_precedes_link_delay_and_sampling_cut():
    simulation, machines, plants, _ = scene(delay=3)
    with simulation:
        simulation.run_until(20)
        assert plants.instances[0].integrated_controls == [0, 0, 7]
        deliveries = [event for event in simulation.events if event.kind == "can.delivery"]
        assert [(event.payload["due_ns"], event.delivery_time_ns) for event in deliveries] == [(13, 13), (13, 13)]
        simulation.step()
        assert plants.instances[0].integrated_controls == [0, 0, 7, 7]


def test_inputs_stable_ties_deep_snapshot_capacity_and_pause_at_completed_cut():
    simulation, machines, _, _ = scene()
    with simulation:
        simulation.max_pending_inputs = 2
        payload = {"value": [1]}
        simulation.schedule_input("first.key", payload, at_ns=0)
        payload["value"].append(9)
        simulation.schedule_input("first.key", {"value": [2]}, at_ns=0)
        with pytest.raises(BufferError, match="input capacity"):
            simulation.schedule_input("first.key", 3)
        simulation.step()
        assert [item[1]["value"] for item in machines.instances[0].input_values] == [(1,), (2,)]
        with pytest.raises(ValueError, match="current dispatch cut"):
            simulation.schedule_input("first.key", 3, at_ns=9)
        simulation.on_event = lambda event: simulation.pause() if event.kind == "machine.completed" else None
        simulation.run_until(100)
        assert simulation.t_sim_ns == 20
        assert simulation.state == SimulationState.PAUSED


def test_bounded_event_history_full_callback_and_immutable_payload():
    events = []
    factory = Factory(lambda spec, context: Component(spec, context))
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory}, event_history=2, on_event=events.append)
    with simulation:
        context = factory.instances[0].context
        payload = {"values": [1, 2]}
        context.emit(None, "sample", payload, source_time_ns=4, mapped_time_ns=7, delivery_time_ns=9, quality="upper_bound")
        payload["values"].append(3)
        context.emit(None, "second", b"x")
        context.emit(None, "third", b"y")
        assert (simulation.events_total, simulation.events_dropped) == (3, 1)
        assert [event.sequence for event in simulation.events] == [1, 2]
        assert len(events) == 3 and events[0].payload["values"] == (1, 2)
        with pytest.raises(TypeError):
            events[0].payload["new"] = 1
        assert (events[0].source_time_ns, events[0].mapped_time_ns, events[0].delivery_time_ns) == (4, 7, 9)


def test_reset_rebuilds_every_owned_instance_and_invalidates_old_context():
    simulation, machine, plant, trace = scene()
    with simulation:
        old = tuple(simulation.components.values())
        context = machine.instances[0].context
        simulation.step()
        simulation.schedule_input("first.key", 1, at_ns=30)
        simulation.reset()
        assert simulation.session.session_id == 2
        assert (simulation.t_sim_ns, simulation.step_id) == (0, 0)
        assert all(before is not after for before, after in zip(old, simulation.components.values()))
        assert all(item.close_count == 1 for item in (machine.instances[0], machine.instances[1], plant.instances[0]))
        assert all(item.sessions == [2] for item in (machine.instances[2], machine.instances[3], plant.instances[1]))
        with pytest.raises(RuntimeError, match="closed session"):
            context.emit(None, "stale", None)
        simulation.run_until(35)
        assert machine.instances[2].input_values == []
        assert simulation.t_sim_ns == 35
    simulation.close()
    assert machine.instances[-1].close_count == 1


def test_failed_step_commits_no_time_freezes_and_independently_cleans_all_owned_components():
    simulation, machines, plants, trace = scene()
    failure = OSError("first completion failed")
    with pytest.raises(OSError, match="first completion failed") as caught:
        with simulation:
            def fail(ctx):
                raise failure
            machines.instances[0].finish_step = fail
            def bad_close():
                trace.append(("close", "bad"))
                raise ValueError("cleanup also failed")
            plants.instances[0].close = bad_close
            try:
                simulation.step()
            except OSError:
                assert simulation.state == SimulationState.FAILED
                assert simulation.t_sim_ns == 0
                with pytest.raises(RuntimeError):
                    simulation.step()
                with pytest.raises(RuntimeError):
                    simulation.reset()
                raise
    assert caught.value is failure
    assert any("cleanup also failed" in note for note in failure.__notes__)
    assert machines.instances[0].close_count == machines.instances[1].close_count == 1
    assert not simulation.components
    simulation.close()


def test_wrong_target_result_is_failure_and_no_later_component_execution():
    simulation, machines, plants, _ = scene()
    with simulation:
        machines.instances[0].finish_step = lambda ctx: StepResult("ok", ctx.t_sim_ns - 1)
        with pytest.raises(RuntimeError, match="exact target"):
            simulation.step()
        assert plants.instances[0].integrated_controls == []
        assert simulation.t_sim_ns == 0


def test_startup_timeout_registers_returned_instance_before_failure_and_cleanup(monkeypatch):
    current = [0.0]
    monkeypatch.setattr(runtime, "monotonic", lambda: current[0])
    def build(spec, context):
        item = Component(spec, context)
        current[0] = 2
        return item
    factory = Factory(build)
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory}, startup_timeout_s=1)
    with pytest.raises(TimeoutError):
        simulation.start()
    assert simulation.state == SimulationState.FAILED
    assert factory.instances[0].close_count == 1
    simulation.close()


def test_active_deadline_checked_after_request_and_cleanup_has_independent_budget(monkeypatch):
    current = [0.0]
    monkeypatch.setattr(runtime, "monotonic", lambda: current[0])
    simulation, machines, plants, _ = scene()
    simulation.host_timeout_s = 1
    with simulation:
        machines.instances[0].request_step = lambda ctx: current.__setitem__(0, 2)
        with pytest.raises(TimeoutError):
            simulation.step()
        assert simulation.t_sim_ns == 0
        assert plants.instances[0].integrated_controls == []
    assert all(item.close_count == 1 for item in machines.instances)


def test_reset_failure_after_old_cleanup_closes_new_partial_instances_and_requires_recreation():
    calls = [0]
    def build(spec, context):
        calls[0] += 1
        item = Component(spec, context)
        if calls[0] == 2:
            def fail_reset(session):
                raise OSError("reset failed")
            item.reset = fail_reset
        return item
    factory = Factory(build)
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory})
    simulation.start()
    with pytest.raises(OSError, match="reset failed"):
        simulation.reset()
    assert [item.close_count for item in factory.instances] == [1, 1]
    assert simulation.state == SimulationState.FAILED
    with pytest.raises(RuntimeError):
        simulation.reset()
    simulation.close()


def test_input_self_excitation_is_bounded_and_failure_not_replayed():
    class Echo(Component):
        def input(self, port, payload):
            self.count += 1
            simulation.schedule_input("n.key", payload)
    factory = Factory(lambda spec, context: Echo(spec, context), {"input"},
                      (PortSpec("key", "digital", "in", "signal"),))
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory}, dispatch_budget=3)
    with simulation:
        factory.instances[0].count = 0
        simulation.schedule_input("n.key", True)
        with pytest.raises(RuntimeError, match="dispatch budget"):
            simulation.step()
        assert factory.instances[0].count == 3
        assert simulation.t_sim_ns == 0


def test_can_self_excitation_and_queue_capacity_are_bounded():
    class Echo(Component):
        def __init__(self, spec, context):
            super().__init__(spec, context)
            self.link = context.attach_can("can", self)
        def receive(self, frame):
            self.link.send(frame)
    factory = Factory(Echo, ports=(PortSpec("can", "can"),))
    topology = Topology((NodeSpec("bus", "bus", {"dispatch_budget": 3, "max_pending": 1}),
                         NodeSpec("a", "echo"), NodeSpec("b", "echo")),
                        (LinkSpec("a.can", "bus.can"), LinkSpec("b.can", "bus.can")))
    simulation = Simulation(topology, {"bus": CanBusFactory(), "echo": factory})
    with simulation:
        factory.instances[0].link.send(CanFrame(1))
        with pytest.raises(BufferError):
            factory.instances[0].link.send(CanFrame(2))
        with pytest.raises(RuntimeError, match="dispatch budget"):
            simulation.step()
        assert simulation.components["bus"].pending == 1


def test_can_interval_admission_preserves_original_frame_and_atomic_overflow():
    class Sink:
        def __init__(self):
            self.frames = []
        def receive(self, frame):
            self.frames.append(frame)
    source, sink = Sink(), Sink()
    events = []
    bus = CanBus(delay_ns=3, log=events.append)
    bus.attach(source)
    bus.attach(sink)
    frame = CanFrame(1, timestamp_ns=2)
    bus.send(source, frame, not_before_ns=10)
    assert bus.step(12) == 0
    assert bus.step(13) == 1
    assert events[0].frame is frame and events[0].timestamp_ns == 2
    assert sink.frames == [CanFrame(1, timestamp_ns=13)]
    with pytest.raises(ValueError, match="64-bit"):
        bus.send(source, frame, not_before_ns=(1 << 64) - 1)
    assert bus.pending == 0
    bus.close()
    bus.close()
    with pytest.raises(RuntimeError, match="closed"):
        bus.send(source, frame)


def test_connected_endpoint_must_be_attached_by_its_factory_before_start_hooks():
    factory = Factory(lambda spec, context: Component(spec, context), ports=(PortSpec("can", "can"),))
    topology = Topology((NodeSpec("bus", "bus"), NodeSpec("n", "n")), (LinkSpec("n.can", "bus.can"),))
    simulation = Simulation(topology, {"bus": CanBusFactory(), "n": factory})
    with pytest.raises(ValueError, match="did not attach"):
        simulation.start()
    assert factory.instances[0].sessions == []
    assert factory.instances[0].close_count == 1
    simulation.close()


def test_invalid_step_admission_is_reversible_and_uint64_overflow_rejected():
    factory = Factory(lambda spec, context: Component(spec, context))
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory},
                            quantum_ns=(1 << 64) - 1)
    with simulation:
        for value in (0, -1, True, 1.5, 1 << 64):
            with pytest.raises(ValueError):
                simulation.step(value)
            assert simulation.state == SimulationState.PAUSED
        simulation.step((1 << 64) - 1)
        with pytest.raises(ValueError):
            simulation.step(1)
        assert simulation.t_sim_ns == (1 << 64) - 1


def test_observer_exception_and_interrupt_remain_primary_and_close_is_idempotent():
    for failure in (ValueError("sink failed"), KeyboardInterrupt()):
        def fail(event):
            raise failure
        simulation, machine, _, _ = scene(on_event=fail)
        with pytest.raises(type(failure)) as caught:
            with simulation:
                simulation.step()
        assert caught.value is failure
        assert all(item.close_count == 1 for item in machine.instances)
        simulation.close()


def test_frozen_record_cannot_hide_mutable_observation_and_startup_reentry_fails_safely():
    @dataclass(frozen=True)
    class BadRecord:
        values: tuple
    factory = Factory(lambda spec, context: Component(spec, context))
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory})
    with simulation:
        with pytest.raises(TypeError, match="immutable fields"):
            factory.instances[0].context.emit(None, "bad", BadRecord(([1],)))
    class Starts(Component):
        def start(self):
            self.context.emit(None, "started", None)
    factory = Factory(Starts)
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory},
                            on_event=lambda event: simulation.start())
    with pytest.raises(RuntimeError, match="new Simulation"):
        simulation.start()
    assert factory.instances[0].close_count == 1
    simulation.close()
