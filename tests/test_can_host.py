import pytest

from sim.api import LinkSpec, NodeSpec, Topology
from sim.devices.can_host import CanHostFactory
from sim.fabric import CanBusFactory, CanFrame
from sim.runtime import Simulation, SimulationState


def graph(**options):
    topology = Topology((NodeSpec("host", "host"), NodeSpec("peer", "host"),
                         NodeSpec("bus", "bus", options)),
                        (LinkSpec("host.can", "bus.can"), LinkSpec("peer.can", "bus.can")))
    return Simulation(topology, {"host": CanHostFactory(), "bus": CanBusFactory()}, quantum_ns=10)


def test_host_input_uses_sole_bus_queue_and_delivery_cut_in_both_directions():
    with graph(delay_ns=2) as sim:
        sim.schedule_input("host.tx", {"id": 0x321, "dlc": 2, "data_hex": "abcd"}, at_ns=3)
        sim.schedule_input("peer.tx", CanFrame(0x322, data=b"ok", timestamp_ns=99), at_ns=7)
        sim.run_until(10)
        received = [event for event in sim.events if event.kind == "can-host.received"]
        assert [(event.source_node, event.payload["frame"], event.source_time_ns, event.delivery_time_ns)
                for event in received] == [
                    ("peer", CanFrame(0x321, dlc=2, data=b"\xab\xcd", timestamp_ns=5), 5, 5),
                    ("host", CanFrame(0x322, data=b"ok", timestamp_ns=9), 9, 9)]
        deliveries = [event for event in sim.events if event.kind == "can.delivery"]
        assert [(event.source_time_ns, event.delivery_time_ns) for event in deliveries] == [(3, 5), (7, 9)]
        assert sim.components["bus"].pending == 0


@pytest.mark.parametrize("payload", [
    {"id": True, "dlc": 0, "data_hex": ""},
    {"id": 1, "dlc": 0, "data_hex": "", "flags": True},
    {"id": 1, "dlc": 1, "data_hex": "aa bb"},
    {"id": 1, "dlc": 1, "data_hex": "a"},
    {"id": 1, "dlc": 1, "data_hex": "zz"},
    {"id": 1, "dlc": 0, "data_hex": "ab"},
    {"id": 0x800, "dlc": 0, "data_hex": ""},
    {"id": 1, "dlc": 0, "data_hex": "", "timestamp_ns": 4},
    b"not a frame",
])
def test_host_frame_admission_is_pure_and_invalid_input_does_not_poison_sim(payload):
    with graph() as sim:
        with pytest.raises(ValueError):
            sim.schedule_input("host.tx", payload)
        assert sim.state == SimulationState.PAUSED
        assert sim.components["bus"].pending == 0
        assert sim.events_total == 0


def test_host_capacity_failure_freezes_and_never_replays_frames():
    sim = graph(max_pending=1)
    with sim:
        host = sim.components["host"]
        sim.schedule_input("host.tx", CanFrame(1))
        sim.schedule_input("host.tx", CanFrame(2))
        with pytest.raises(BufferError):
            sim.run_until(10)
        assert sim.state == SimulationState.FAILED
        assert sim.components["bus"].pending == 1
        assert not any(event.kind == "can-host.received" for event in sim.events)
        with pytest.raises(RuntimeError):
            sim.run_until(10)
    with pytest.raises(RuntimeError, match="closed"):
        host.input("tx", CanFrame(1))


def test_host_requires_one_existing_can_owner_before_resource_acquisition():
    with pytest.raises(ValueError):
        Simulation(Topology((NodeSpec("host", "host"),)), {"host": CanHostFactory()})


def test_host_external_tx_cannot_claim_an_unimplemented_connection():
    from sim.api import NodeDescription, PortSpec
    class Owner:
        def describe(self, spec):
            return NodeDescription((PortSpec("tx", "can-frame", "out", "signal"),), {"connection"})
        def create(self, *_):
            pytest.fail("static admission acquired resources")
    topology = Topology((NodeSpec("host", "host"), NodeSpec("bus", "bus"), NodeSpec("source", "owner")),
                        (LinkSpec("host.can", "bus.can"), LinkSpec("source.tx", "host.tx")))
    with pytest.raises(ValueError, match="external input only"):
        Simulation(topology, {"host": CanHostFactory(), "bus": CanBusFactory(), "owner": Owner()})
