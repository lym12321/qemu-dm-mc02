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

    def validate_input(self, spec, port, payload, *, connected_ports):
        return payload

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
        self.connection = context.attach("can", self)

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
        self.connection = context.attach("can", self)

    def receive(self, frame):
        self.control = frame.data[0]

    def step(self, ctx):
        self.integrated_controls.append(self.control)
        self.context.emit("can", "plant.completed", {"control": self.control})
        return StepResult("ok", ctx.t_sim_ns)


def scene(*, delay=0, on_event=None, mode="stepped"):
    trace = []
    machine = Factory(lambda spec, ctx: Machine(spec, ctx, trace, send=spec.name == "first"),
                      {"machine", "input"}, (PortSpec("can", "can"), PortSpec("key", "digital", "in", "signal", capabilities={"external_input"})))
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
                      (PortSpec("key", "digital", "in", "signal", capabilities={"external_input"}),))
    simulation = Simulation(Topology((NodeSpec("plant", "plant"),)),
                            {"plant": factory}, quantum_ns=10, **kwargs)
    return simulation, factory


def test_generic_connection_owners_sample_all_sources_before_dispatching_sinks():
    trace = []

    class Owner(Component):
        dispatch_budget = 4

        def __init__(self, spec, context):
            super().__init__(spec, context, trace)
            trace.append(("create", self.name))
            self.queue = []

        def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
            assert descriptor.name == "p" and descriptor.protocol == "opaque"
            assert endpoint_name == f"{context.node_id}.p" and owner_port == "wire"
            self.endpoint = endpoint
            return self

        def start(self):
            trace.append(("start", self.name))

        def sample(self, cut):
            trace.append(("sample", self.name, cut))
            self.queue.append(cut)

        @property
        def next_due_ns(self):
            return self.queue[0] if self.queue else None

        def dispatch(self, cut, budget):
            if not self.queue or not budget:
                return 0
            trace.append(("dispatch", self.name, cut))
            self.endpoint.receive(self.queue.pop(0))
            return 1

    class Consumer(Component):
        def __init__(self, spec, context):
            super().__init__(spec, context, trace)
            trace.append(("create", self.name))
            self.connection = context.attach("p", self)
            self.received = []

        def start(self):
            trace.append(("start", self.name))

        def receive(self, value):
            self.received.append(value)

        def step(self, ctx):
            trace.append(("step", self.name))
            return StepResult("ok", ctx.t_sim_ns)

    owner = Factory(Owner, {"connection", "dispatch", "sample"},
                    (PortSpec("wire", "opaque", max_connections=None),))
    consumer = Factory(Consumer, {"step"}, (PortSpec("p", "opaque"),))
    topology = Topology(tuple(NodeSpec(name, kind) for name, kind in
        (("first", "consumer"), ("wire1", "owner"), ("second", "consumer"), ("wire2", "owner"))),
        (LinkSpec("first.p", "wire1.wire"), LinkSpec("second.p", "wire2.wire")))
    with Simulation(topology, {"owner": owner, "consumer": consumer}) as simulation:
        assert trace[:4] == [("create", "wire1"), ("create", "wire2"),
                             ("create", "first"), ("create", "second")]
        assert all(item.received == [0] for item in consumer.instances)
        simulation.run_until(0)
        simulation.step(3)
        assert all(item.received == [0, 3] for item in consumer.instances)
        for cut in (0, 3):
            samples = [trace.index(("sample", name, cut)) for name in ("wire1", "wire2")]
            deliveries = [trace.index(("dispatch", name, cut)) for name in ("wire1", "wire2")]
            assert max(samples) < min(deliveries)
            completed = [index for index, item in enumerate(trace)
                         if item[0] == ("start" if cut == 0 else "step")]
            assert max(completed) < min(samples)
        assert len([item for item in trace if item[0] == "sample"]) == 4


def binding_scene(*, attach_protocol=True, attach_binding=True):
    """Only record generic ownership; no solver or device behavior is mocked."""
    trace = []

    class Connection(Component):
        def __init__(self, spec, context):
            super().__init__(spec, context, trace)
            trace.append(("create", self.name))
            self.endpoints = []

        def attach_port(self, endpoint, **kwargs):
            assert kwargs["descriptor"].category == "protocol"
            self.endpoints.append(kwargs["endpoint_name"])
            return self

    class BindingOwner(Component):
        def __init__(self, spec, context):
            super().__init__(spec, context, trace)
            trace.append(("create", self.name))
            self.bindings = []
            self.targets = []
            if attach_protocol or self.name != "world_a":
                self.connection = context.attach("p", self)

        def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
            assert descriptor.category == "binding" and owner_port == "bindings"
            assert endpoint_name == f"{context.node_id}.binding"
            self.bindings.append(endpoint)
            return self

        def reset(self, session):
            super().reset(session)
            trace.append(("reset", self.name))
            self.reset_bindings = tuple(item.name for item in self.bindings)

        def step(self, ctx):
            self.targets.append(ctx.t_sim_ns)
            return StepResult("ok", ctx.t_sim_ns)

    class Borrower(Component):
        def __init__(self, spec, context):
            super().__init__(spec, context, trace)
            trace.append(("create", self.name))
            if attach_binding or self.name != "extra_a":
                self.binding = context.attach("binding", self)

    connection = Factory(Connection, {"connection"},
                         (PortSpec("wire", "opaque", max_connections=None),))
    owner = Factory(BindingOwner, {"binding_owner", "step"},
                    (PortSpec("p", "opaque"),
                     PortSpec("bindings", "kinematic", category="binding", max_connections=None)))
    borrower = Factory(Borrower, ports=(PortSpec("binding", "kinematic", category="binding"),))
    topology = Topology(tuple(NodeSpec(name, kind) for name, kind in (
        ("client_b", "borrower"), ("world_a", "owner"), ("wire_b", "connection"),
        ("client_a", "borrower"), ("wire_a", "connection"), ("world_b", "owner"),
        ("extra_a", "borrower"))),
        (LinkSpec("world_a.p", "wire_a.wire"), LinkSpec("wire_b.wire", "world_b.p"),
         LinkSpec("client_b.binding", "world_b.bindings"),
         LinkSpec("world_a.bindings", "client_a.binding"),
         LinkSpec("extra_a.binding", "world_a.bindings")))
    return Simulation(topology, {"connection": connection, "owner": owner, "borrower": borrower}), trace


def test_binding_owners_step_once_with_stable_construction_order_and_borrowed_lifecycle():
    simulation, trace = binding_scene()
    with simulation:
        expected_order = ("wire_b", "wire_a", "world_a", "world_b", "client_b", "client_a", "extra_a")
        assert tuple(name for phase, name in trace if phase == "create") == expected_order
        first = dict(simulation.components)
        assert first["wire_a"].endpoints == ["world_a.p"]
        assert first["wire_b"].endpoints == ["world_b.p"]
        assert first["world_a"].reset_bindings == ("client_a", "extra_a")
        assert first["world_b"].reset_bindings == ("client_b",)
        assert first["client_a"].binding is first["extra_a"].binding is first["world_a"]
        assert first["client_b"].binding is first["world_b"]
        simulation.step(4)
        simulation.step(3)
        assert first["world_a"].targets == first["world_b"].targets == [4, 7]
        simulation.reset()
        assert simulation.session.session_id == 2
        assert (simulation.t_sim_ns, simulation.step_id) == (0, 0)
        assert tuple(name for phase, name in trace if phase == "close") == expected_order[::-1]
        assert all(component.close_count == 1 for component in first.values())
        assert all(component is not simulation.components[name] for name, component in first.items())
        for name in ("world_a", "client_a"):
            with pytest.raises(RuntimeError, match="closed session"):
                first[name].context.check_io()
            with pytest.raises(RuntimeError, match="closed session"):
                first[name].context.attach("binding", object())
        simulation.step(2)
        second = dict(simulation.components)
        assert second["world_a"].targets == second["world_b"].targets == [2]
        assert all(component.sessions == [2] for component in second.values())
    assert all(component.close_count == 1 for component in second.values())


@pytest.mark.parametrize("options,endpoint", [
    ({"attach_protocol": False}, "world_a.p"),
    ({"attach_binding": False}, "extra_a.binding"),
])
def test_each_borrower_endpoint_must_attach_regardless_of_its_other_owner_capabilities(options, endpoint):
    simulation, trace = binding_scene(**options)
    with pytest.raises(ValueError, match=f"did not attach its connected endpoint: {endpoint}"):
        simulation.start()
    assert simulation.state == SimulationState.FAILED
    assert len([item for item in trace if item[0] == "create"]) == 7
    assert len([item for item in trace if item[0] == "close"]) == 7
    assert not any(item[0] == "reset" for item in trace)
    assert not simulation.components
    simulation.close()


def test_missing_binding_owner_attach_method_is_admitted_after_return_then_cleaned():
    class Incomplete(Component):
        def step(self, ctx):
            raise AssertionError("invalid binding owner stepped")

    owner = Factory(Incomplete, {"binding_owner", "step"},
                    (PortSpec("p", "kinematic", category="binding"),))
    borrower = Factory(Component, ports=(PortSpec("p", "kinematic", category="binding"),))
    topology = Topology((NodeSpec("client", "client"), NodeSpec("owner", "owner")),
                        (LinkSpec("client.p", "owner.p"),))
    simulation = Simulation(topology, {"client": borrower, "owner": owner})
    with pytest.raises(TypeError, match="lacks capability methods:.*attach_port"):
        simulation.start()
    assert owner.instances[0].sessions == []
    assert owner.instances[0].close_count == 1
    assert borrower.instances == []
    assert simulation.state == SimulationState.FAILED
    simulation.close()


@pytest.mark.parametrize("failure", ["bad_due", "bad_due_after_dispatch", "negative_count",
                                     "boolean_count", "excess_count", "no_progress"])
def test_dispatch_contract_failures_freeze_and_cleanup_without_retry(failure):
    class Dispatcher(Component):
        dispatch_budget = 2
        next_due_ns = None
        calls = 0
        enabled = False

        def dispatch(self, cut, budget):
            if not self.enabled:
                return 0
            self.calls += 1
            if failure == "bad_due_after_dispatch":
                self.next_due_ns = 1 << 64
                return 1
            return {"negative_count": -1, "boolean_count": True,
                    "excess_count": 3, "no_progress": 0}[failure]

    factory = Factory(Dispatcher, {"dispatch"})
    simulation = Simulation(Topology((NodeSpec("dispatch", "dispatch"),)), {"dispatch": factory})
    with simulation:
        item = factory.instances[0]
        item.enabled = True
        item.next_due_ns = -1 if failure == "bad_due" else 0
        with pytest.raises((ValueError, RuntimeError)):
            simulation.run_until(0)
        assert item.calls == (0 if failure == "bad_due" else 1)
        assert (simulation.t_sim_ns, simulation.step_id) == (0, 0)
        assert simulation.state == SimulationState.FAILED
        with pytest.raises(RuntimeError):
            simulation.step()
    assert item.close_count == 1


def test_bad_dispatcher_admission_closes_returned_owner_before_any_start():
    class Invalid(Component):
        dispatch_budget = True
        next_due_ns = None

        def dispatch(self, cut, budget):
            raise AssertionError("invalid owner dispatched")

    factory = Factory(Invalid, {"dispatch"})
    simulation = Simulation(Topology((NodeSpec("n", "n"),)), {"n": factory})
    with pytest.raises(ValueError, match="dispatch_budget"):
        simulation.start()
    assert factory.instances[0].sessions == []
    assert factory.instances[0].close_count == 1
    assert simulation.state == SimulationState.FAILED
    simulation.close()


@pytest.mark.parametrize("operation", ["start", "reset"])
@pytest.mark.parametrize("callback_phase", ["sample", "delivery"])
def test_initial_sample_and_delivery_reject_close_reentry_and_preserve_cleanup(operation, callback_phase):
    rejected = []
    enabled = False
    failing_session = 1 if operation == "start" else 2

    class InitialEvents(Component):
        dispatch_budget = 1
        next_due_ns = None

        def sample(self, cut):
            self.context.emit(None, "startup.sample", cut)
            self.next_due_ns = cut

        def dispatch(self, cut, budget):
            if self.next_due_ns is None:
                return 0
            self.context.emit(None, "startup.delivery", cut)
            self.next_due_ns = None
            return 1

        def close(self):
            super().close()
            if self.context.session.session_id == failing_session:
                raise ValueError("initial cleanup also failed")

    events = Factory(InitialEvents, {"sample", "dispatch"})
    probe = Factory(lambda spec, context: Component(spec, context))
    topology = Topology((NodeSpec("probe", "probe"), NodeSpec("events", "events")))

    def attempt_close(event):
        if enabled and event.kind == f"startup.{callback_phase}":
            try:
                simulation.close()
            except RuntimeError as exc:
                rejected.append(exc)
                raise

    simulation = Simulation(topology, {"probe": probe, "events": events}, on_event=attempt_close)
    if operation == "reset":
        simulation.start()
    enabled = True
    with pytest.raises(RuntimeError, match="active construction") as caught:
        getattr(simulation, operation)()
    assert caught.value is rejected[0]
    assert any("initial cleanup also failed" in note for note in caught.value.__notes__)
    assert all(item.close_count == 1 for item in (*events.instances, *probe.instances))
    assert simulation.state == SimulationState.FAILED
    assert not simulation.components
    simulation.close()


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
            self.link = context.attach("can", self)
            self.inputs = self.received = 0

        def input(self, port, payload):
            self.inputs += 1
            self.link.send(CanFrame(1))

        def receive(self, frame):
            self.received += 1
            simulation.schedule_input("n.key", 1)

    factory = Factory(Alternating, {"input"},
                      (PortSpec("can", "can"), PortSpec("key", "digital", "in", "signal", capabilities={"external_input"})))
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
                      (PortSpec("key", "digital", "in", "signal", capabilities={"external_input"}),))
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
            self.link = context.attach("can", self)
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
