"""CAN fault admission and direct owner policy over the canonical bus queue.

These are deterministic host delivery tests, without physical CAN fault claims.
The scene and native gates exercise separate direct consumers.
"""

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, Topology
from sim.devices.can_host import CanHostFactory
from sim.fabric import CanBusFactory, CanFrame
from sim.fabric.factory import CanNetwork
from sim.runtime import Simulation


class Context:
    def __init__(self):
        self.t_sim_ns = 0
        self.events = []
        self.on_emit = None
        self.io_checks = 0

    def check_io(self):
        self.io_checks += 1

    def check_deadline(self):
        pass

    def emit(self, port, kind, payload, **times):
        self.events.append((port, kind, payload, times))
        if self.on_emit is not None:
            self.on_emit(kind)


class Endpoint:
    # Deliberately equal and unhashable: names must follow endpoint identity.
    def __init__(self):
        self.frames = []
        self.closed = 0

    def __eq__(self, other):
        return isinstance(other, Endpoint)

    def receive(self, frame):
        self.frames.append(frame)

    def close(self):
        self.closed += 1


def network(profiles, *, delay_ns=0, drop=None, on_delivery=None, **options):
    context = Context()
    owner = CanNetwork(NodeSpec("bus", "bus", dict(
        fault_profiles=profiles, delay_ns=delay_ns, **options)), context,
        drop=drop, on_delivery=on_delivery)
    peers = [Endpoint(), Endpoint(), Endpoint()]
    for name, endpoint in zip(("source.can", "first.can", "second.can"), peers):
        owner.attach_port(endpoint, descriptor=PortSpec("can", "can"),
                          endpoint_name=name, owner_port="can", context=context)
    return owner, context, peers


def spec(profiles):
    return NodeSpec("bus", "bus", {"fault_profiles": profiles})


@pytest.mark.parametrize("profiles", [
    [], {"normal": {}}, {"": {}}, {"é": {}}, {"a.b": {}}, {"x" * 65: {}},
    {str(i): {} for i in range(17)},
    {"bad": []}, {"bad": {"unknown": 1}},
    {"bad": {"delay_ns": True}}, {"bad": {"delay_ns": -1}},
    {"bad": {"delay_ns": 1 << 64}}, {"bad": {"drop": {}}},
    {"bad": {"drop": [{}]}}, {"bad": {"drop": [{"unknown": 1}]}},
    {"bad": {"drop": [{"source": "unqualified"}]}},
    {"bad": {"drop": [{"destination": 1}]}},
    {"bad": {"drop": [{"can_id": True}]}},
    {"bad": {"drop": [{"can_id": -1}]}},
    {"bad": {"drop": [{"can_id": 0x20000000}]}},
    {"first": {"drop": [{"can_id": 1}] * 33},
     "second": {"drop": [{"can_id": 2}] * 32}},
])
def test_invalid_fault_schema_rejects_before_acquisition(profiles):
    class NeverCreate(CanBusFactory):
        def create(self, *_):
            pytest.fail("invalid admission acquired a resource")

    with pytest.raises(ValueError):
        Simulation(Topology((spec(profiles),)), {"bus": NeverCreate()})


def test_bounded_profiles_and_rules_are_accepted_at_the_limits():
    profiles = {f"p{i}": {"drop": [{"can_id": 0}, {"can_id": 0x1fffffff},
                                    {"can_id": 1}, {"can_id": 2}]}
                for i in range(16)}
    description = CanBusFactory.describe(spec(profiles))
    assert description.port("fault").capabilities == {"external_input"}
    assert "input" in description.capabilities


@pytest.mark.parametrize("options", [{}, {"fault_profiles": {}}])
def test_unconfigured_bus_has_no_external_fault_capability(options):
    description = CanBusFactory.describe(NodeSpec("bus", "bus", options))
    assert description.capabilities == {"connection", "dispatch"}
    with pytest.raises(ValueError, match="unknown port"):
        description.port("fault")


@pytest.mark.parametrize("payload", [None, True, {}, ["broken"], "unknown"])
def test_profile_input_is_pure_and_only_accepts_declared_names(payload):
    node = spec({"broken": {}})
    with pytest.raises(ValueError, match="profile name"):
        CanBusFactory.validate_input(node, "fault", payload, connected_ports=())
    assert CanBusFactory.validate_input(node, "fault", "normal", connected_ports=()) == "normal"
    assert CanBusFactory.validate_input(node, "fault", "broken", connected_ports=()) == "broken"
    with pytest.raises(ValueError, match="external input only"):
        CanBusFactory.validate_input(node, "fault", "normal", connected_ports=("fault",))


@pytest.mark.parametrize("endpoint", ["missing.can", "peer.tx", "other.can", "bus.can"])
def test_drop_endpoint_must_be_a_real_peer_on_the_same_bus(endpoint):
    nodes = (spec({"broken": {"drop": [{"destination": endpoint}]}}),
             NodeSpec("peer", "host"), NodeSpec("other", "host"), NodeSpec("bus2", "bus"))
    graph = Topology(nodes, (LinkSpec("peer.can", "bus.can"), LinkSpec("other.can", "bus2.can")))
    with pytest.raises(ValueError, match="not attached to bus"):
        Simulation(graph, {"bus": CanBusFactory(), "host": CanHostFactory()})


def test_fault_port_cannot_claim_a_connection_owner():
    class Source:
        def describe(self, _spec):
            return NodeDescription((PortSpec("fault", "can-fault", "out", "signal"),), {"connection"})

        def create(self, *_):
            pytest.fail("external-only link acquired a resource")

    graph = Topology((spec({"broken": {}}), NodeSpec("source", "source")),
                     (LinkSpec("source.fault", "bus.fault"),))
    with pytest.raises(ValueError, match="external input only"):
        Simulation(graph, {"bus": CanBusFactory(), "source": Source()})


def test_resolved_event_is_immutable_and_follows_effects_and_actual_cut():
    rules = [{"destination": "first.can", "can_id": 0x201}]
    owner, context, _ = network({"broken": {"drop": rules}}, delay_ns=7)
    rules[0]["can_id"] = 0x202
    observed = []
    context.on_emit = lambda _kind: observed.append((owner.bus.delay_ns, owner._profile))
    context.t_sim_ns = 13
    owner.input("fault", "broken")
    assert observed == [(7, "broken")]
    assert context.events == [("fault", "can.fault.applied", {
        "previous_profile": "normal", "profile": "broken", "delay_ns": 7,
        "drop": ({"destination": "first.can", "can_id": 0x201},), "cut_ns": 13,
    }, {"source_time_ns": 13, "mapped_time_ns": 13, "delivery_time_ns": 13})]
    with pytest.raises(TypeError):
        context.events[0][2]["drop"][0]["can_id"] = 0
    owner.input("fault", "normal")
    assert context.events[-1][2]["previous_profile"] == "broken"
    assert context.events[-1][2]["drop"] == ()
    owner.close()


def test_delay_change_preserves_queued_due_and_new_ties_keep_enqueue_order():
    owner, context, (source, first, _) = network({"fast": {"delay_ns": 0}}, delay_ns=10)
    owner.bus.send(source, CanFrame(1, timestamp_ns=5))
    context.t_sim_ns = 4
    owner.input("fault", "fast")
    assert owner.next_due_ns == 15
    owner.bus.send(source, CanFrame(2, timestamp_ns=6))
    owner.bus.send(source, CanFrame(3, timestamp_ns=15))
    assert owner.dispatch(6, 10) == 1
    assert owner.dispatch(15, 10) == 2
    assert first.frames == [CanFrame(2, timestamp_ns=6), CanFrame(1, timestamp_ns=15),
                            CanFrame(3, timestamp_ns=15)]
    receipts = [event[2] for event in context.events
                if event[1] == "can.delivery" and event[2]["destination"] == "first.can"]
    assert [(r["frame"].timestamp_ns, r["due_ns"], r["bus_sequence"])
            for r in receipts] == [(6, 6, 1), (5, 15, 0), (15, 15, 2)]
    owner.close()


def test_drop_is_dispatch_policy_with_scoped_and_rules_or_and_no_replay():
    owner, context, (source, first, second) = network({"broken": {"drop": [
        {"source": "source.can", "destination": "first.can", "can_id": 0x201},
        {"source": "second.can", "destination": "source.can"},
    ]}}, delay_ns=5)
    owner.bus.send(source, CanFrame(0x201, data=b"old", timestamp_ns=2))
    context.t_sim_ns = 6
    owner.input("fault", "broken")
    owner.bus.send(source, CanFrame(0x202, data=b"other", timestamp_ns=2))
    owner.bus.send(source, CanFrame(0x201, flags=1, data=b"extended", timestamp_ns=2))
    owner.bus.send(second, CanFrame(0x203, data=b"reverse", timestamp_ns=2))
    assert owner.dispatch(7, 10) == 4
    assert [(f.can_id, f.data) for f in first.frames] == [(0x202, b"other"), (0x203, b"reverse")]
    assert [(f.can_id, f.flags, f.data) for f in second.frames] == [
        (0x201, 0, b"old"), (0x202, 0, b"other"), (0x201, 1, b"extended")]
    assert source.frames == []
    context.t_sim_ns = 8
    owner.input("fault", "normal")
    assert owner.dispatch(20, 10) == 0
    owner.bus.send(source, CanFrame(0x201, data=b"new", timestamp_ns=20))
    assert owner.dispatch(25, 10) == 1
    assert first.frames[-1] == CanFrame(0x201, data=b"new", timestamp_ns=25)
    owner.close()


def test_inherited_callback_runs_once_per_destination_even_if_declared_drop_matches():
    calls = []
    owner, context, (source, first, second) = network(
        {"broken": {"drop": [{"destination": "first.can"}]}},
        drop=lambda src, dst, frame: calls.append((src, dst, frame)) or dst is second)
    owner.input("fault", "broken")
    original = CanFrame(1, data=b"opaque", timestamp_ns=3)
    owner.bus.send(source, original)
    assert owner.dispatch(3, 10) == 1
    assert len(calls) == 2
    assert calls[0][0] is source and calls[0][1] is first
    assert calls[1][0] is source and calls[1][1] is second
    assert all(call[2] is original for call in calls)
    assert first.frames == second.frames == []
    assert [event[2]["dropped"] for event in context.events if event[1] == "can.delivery"] == [True, True]
    owner.close()


def test_callback_error_is_primary_and_failed_frame_is_never_replayed():
    failure = OSError("borrowed callback failed")
    calls = []

    def drop(*args):
        calls.append(args)
        raise failure

    owner, _, (source, first, second) = network(
        {"broken": {"drop": [{"can_id": 1}]}}, drop=drop)
    owner.input("fault", "broken")
    owner.bus.send(source, CanFrame(1))
    owner.bus.send(source, CanFrame(2))
    with pytest.raises(OSError) as caught:
        owner.dispatch(0, 10)
    assert caught.value is failure
    assert len(calls) == 1 and owner.pending == 1
    with pytest.raises(OSError) as caught:
        owner.input("fault", "normal")
    assert caught.value is failure
    with pytest.raises(OSError) as caught:
        owner.dispatch(1, 10)
    assert caught.value is failure and len(calls) == 1
    assert first.frames == second.frames == []
    owner.close()
    assert owner.pending == 0 and source.closed == first.closed == second.closed == 0


@pytest.mark.parametrize("boundary", ["event", "delivery_callback"])
def test_delivery_observer_failure_freezes_before_receive_until_successful_reset(boundary):
    failure = OSError("delivery observation failed")
    observed = []

    def reject(_value):
        observed.append(boundary)
        raise failure

    owner, context, (source, first, second) = network(
        {"broken": {"delay_ns": 3}}, delay_ns=1,
        on_delivery=reject if boundary == "delivery_callback" else None)
    owner.input("fault", "broken")
    if boundary == "event":
        context.on_emit = reject
    owner.bus.send(source, CanFrame(1))
    owner.bus.send(source, CanFrame(2))
    with pytest.raises(OSError) as caught:
        owner.dispatch(3, 10)
    assert caught.value is failure and observed == [boundary]
    assert owner.pending == 1 and first.frames == second.frames == []
    with pytest.raises(OSError) as caught:
        owner.input("fault", "normal")
    assert caught.value is failure
    context.on_emit = None
    owner._on_delivery = None
    owner.reset(Session(2, 10))
    assert owner.pending == 0 and owner.bus.delay_ns == 1
    assert owner.dispatch(10, 10) == 0
    owner.bus.send(source, CanFrame(3, timestamp_ns=10))
    assert owner.dispatch(11, 10) == 1
    assert first.frames == second.frames == [CanFrame(3, timestamp_ns=11)]
    owner.close()


@pytest.mark.parametrize("operation", ["input", "reset", "close", "dispatch"])
def test_swallowed_policy_or_lifecycle_reentry_freezes_before_peer_delivery(operation):
    errors = []
    owner = None

    def drop(*_args):
        try:
            {"input": lambda: owner.input("fault", "normal"),
             "reset": lambda: owner.reset(Session(2, 0)),
             "close": owner.close,
             "dispatch": lambda: owner.dispatch(0, 1)}[operation]()
        except RuntimeError as error:
            errors.append(error)
        return False

    owner, _, (source, first, second) = network({"broken": {}}, drop=drop)
    owner.bus.send(source, CanFrame(1))
    with pytest.raises(RuntimeError) as caught:
        owner.dispatch(0, 10)
    assert caught.value is errors[0]
    assert first.frames == second.frames == []
    owner.close()


def test_observer_failure_preserves_applied_effects_and_reset_restores_base():
    failure = OSError("fault observer failed after policy change")
    owner, context, (source, first, _) = network(
        {"broken": {"delay_ns": 9, "drop": [{"can_id": 1}]}}, delay_ns=2)
    context.on_emit = lambda _kind: (_ for _ in ()).throw(failure)
    with pytest.raises(OSError) as caught:
        owner.input("fault", "broken")
    assert caught.value is failure and owner.bus.delay_ns == 9
    with pytest.raises(OSError) as caught:
        owner.dispatch(0, 10)
    assert caught.value is failure
    context.on_emit = None
    owner.reset(Session(2, 100))
    assert owner.bus.delay_ns == 2
    owner.bus.send(source, CanFrame(1, timestamp_ns=100))
    assert owner.dispatch(102, 10) == 1
    assert first.frames == [CanFrame(1, timestamp_ns=102)]
    owner.close()
    with pytest.raises(RuntimeError, match="closed"):
        owner.input("fault", "normal")


def test_fault_observer_cannot_hide_reentrant_control_or_continue_execution():
    owner, context, _ = network({"broken": {"delay_ns": 9}}, delay_ns=2)
    errors = []

    def observe(_kind):
        try:
            owner.input("fault", "normal")
        except RuntimeError as error:
            errors.append(error)

    context.on_emit = observe
    with pytest.raises(RuntimeError) as caught:
        owner.input("fault", "broken")
    assert caught.value is errors[0] and owner.bus.delay_ns == 9
    assert len(context.events) == 1
    owner.close()
