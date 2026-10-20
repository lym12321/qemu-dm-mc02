"""Pure topology admission never creates an execution component."""

from pathlib import Path

import pytest

from sim.api import Endpoint, LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.fabric import CanBusFactory
from sim.runtime import Simulation


class Factory:
    def __init__(self, *ports, capabilities=()):
        self.description = NodeDescription(ports, frozenset(capabilities))
        self.created = 0

    def describe(self, spec):
        return self.description

    def create(self, spec, context):
        self.created += 1
        raise AssertionError("static admission must never create resources")


def pair(first, second, *, links=None):
    return Topology((NodeSpec("a", "a"), NodeSpec("b", "b")),
                    tuple(links if links is not None else [LinkSpec("a.p", "b.p")]))


def test_static_validation_freezes_source_data_and_preserves_three_connection_categories(tmp_path):
    options = {"nested": [1, {"value": "original"}]}
    node = NodeSpec("a", "signal", options, base_dir=tmp_path)
    options["nested"][1]["value"] = "changed"
    assert node.options["nested"][1]["value"] == "original"
    assert node.base_dir == tmp_path.resolve()
    with pytest.raises(TypeError):
        node.options["new"] = 3
    for category, protocol in (("protocol", "uart"), ("signal", "digital"), ("binding", "joint")):
        a = Factory(PortSpec("p", protocol, "out", category))
        b = Factory(PortSpec("p", protocol, "in", category))
        descriptions = pair(a, b).validate({"a": a, "b": b})
        assert descriptions["a"].ports[0].category == category
        assert a.created == b.created == 0
        with pytest.raises(TypeError):
            descriptions["extra"] = NodeDescription()


@pytest.mark.parametrize("ports,links,error", [
    ((PortSpec("p", "can"), PortSpec("p", "uart")), None, "protocol mismatch"),
    ((PortSpec("p", "digital", category="signal"), PortSpec("p", "digital")), None, "type/protocol mismatch"),
    ((PortSpec("p", "uart", "out"), PortSpec("p", "uart", "out")), None, "direction mismatch"),
    ((PortSpec("p", "uart", "in"), PortSpec("p", "uart", "in")), None, "direction mismatch"),
    ((PortSpec("p", "uart"), PortSpec("q", "uart")), None, "unknown port"),
    ((PortSpec("p", "uart"), PortSpec("p", "uart")), [LinkSpec("a.p", "missing.p")], "unknown node"),
    ((PortSpec("p", "uart"), PortSpec("p", "uart")), [LinkSpec("a.p", "a.p")], "itself"),
    ((PortSpec("p", "uart"), PortSpec("p", "uart")), [LinkSpec("a.p", "b.p"), LinkSpec("b.p", "a.p")], "duplicate connection"),
])
def test_bad_links_rejected_before_create(ports, links, error):
    a, b = Factory(ports[0]), Factory(ports[1])
    with pytest.raises(ValueError, match=error):
        pair(a, b, links=links).validate({"a": a, "b": b})
    assert a.created == b.created == 0


def test_missing_required_capabilities_and_port_capacity():
    with pytest.raises(ValueError, match="lacks required capabilities"):
        Topology((NodeSpec("a", "a", required_capabilities={"execute"}),)).validate({"a": Factory()})
    a = Factory(PortSpec("p", "uart", required_capabilities={"buffered"}))
    b = Factory(PortSpec("p", "uart"))
    with pytest.raises(ValueError, match="required capabilities"):
        pair(a, b).validate({"a": a, "b": b})
    a = Factory(PortSpec("p", "uart", capabilities={"buffered"}))
    b = Factory(PortSpec("p", "uart", capabilities={"buffered"}))
    assert pair(a, b, links=[LinkSpec("a.p", "b.p", {"buffered"})]).validate({"a": a, "b": b})
    topology = Topology((NodeSpec("a", "a"), NodeSpec("b", "b"), NodeSpec("c", "b")),
                        (LinkSpec("a.p", "b.p"), LinkSpec("a.p", "c.p")))
    with pytest.raises(ValueError, match="capacity exceeded"):
        topology.validate({"a": a, "b": b})


def test_unknown_factory_duplicate_node_and_invalid_descriptors():
    with pytest.raises(ValueError, match="unknown component"):
        Topology((NodeSpec("a", "missing"),)).validate({})
    with pytest.raises(ValueError, match="duplicate node"):
        Topology((NodeSpec("a", "a"), NodeSpec("a", "a")))
    with pytest.raises(ValueError, match="duplicate port"):
        NodeDescription((PortSpec("p", "can"), PortSpec("p", "can")))
    for kwargs in ({"direction": "sideways"}, {"category": "raw"}, {"max_connections": False}):
        with pytest.raises(ValueError):
            PortSpec("p", "can", **kwargs)
    with pytest.raises(ValueError):
        NodeSpec("a.b", "machine")
    assert Endpoint.parse("main.gpio.A15") == Endpoint("main", "gpio.A15")


def test_simulation_static_capabilities_and_can_wiring_admission():
    with pytest.raises(ValueError, match="both machine"):
        Simulation(Topology((NodeSpec("a", "a"),)), {"a": Factory(capabilities={"machine", "step"})})
    a = Factory(PortSpec("p", "can"))
    with pytest.raises(ValueError, match="one bus and one endpoint"):
        Simulation(pair(a, a), {"a": a, "b": a})
    for options in ({"unknown": 1}, {"delay_ns": True}, {"max_pending": 0}, {"dispatch_budget": 65537}):
        with pytest.raises(ValueError):
            Simulation(Topology((NodeSpec("bus", "bus.can", options),)), {"bus.can": CanBusFactory()})
    simulation = Simulation(Topology((NodeSpec("bus", "bus.can"),)), {"bus.can": CanBusFactory()})
    assert simulation.validate()["bus"].capabilities == frozenset({"can_bus"})
    assert not simulation.components
    simulation.close()


def test_required_connection_and_unimplemented_typed_links_rejected_before_resources():
    required = Factory(PortSpec("p", "can", min_connections=1))
    with pytest.raises(ValueError, match="requires at least 1"):
        Topology((NodeSpec("a", "a"),)).validate({"a": required})
    assert required.created == 0
    for category, protocol in (("protocol", "uart"), ("signal", "digital"), ("binding", "joint")):
        a = Factory(PortSpec("p", protocol, "out", category))
        b = Factory(PortSpec("p", protocol, "in", category))
        with pytest.raises(ValueError, match="runtime does not support linked"):
            Simulation(pair(a, b), {"a": a, "b": b})
        assert a.created == b.created == 0
    a = Factory(PortSpec("p", "can", direction="in"))
    topology = Topology((NodeSpec("a", "a"), NodeSpec("bus", "bus")), (LinkSpec("a.p", "bus.can"),))
    with pytest.raises(ValueError, match="inout"):
        Simulation(topology, {"a": a, "bus": CanBusFactory()})


def test_pure_topology_admission_hook_runs_once_per_factory_object_across_kind_aliases():
    class Shared(Factory):
        def __init__(self):
            super().__init__()
            self.checked = []
        def validate_topology(self, topology, descriptions, registry):
            self.checked.append(tuple(node.name for node in topology.nodes))
            assert set(descriptions) == {"a", "b", "c"}
            with pytest.raises(TypeError):
                registry["new"] = self
    first, second = Shared(), Shared()
    topology = Topology((NodeSpec("a", "one"), NodeSpec("b", "alias"), NodeSpec("c", "two")))
    topology.validate({"one": first, "alias": first, "two": second})
    assert first.checked == second.checked == [("a", "b", "c")]
    assert first.created == second.created == 0


def test_fixed_rotor_duplicate_address_rejected_before_io_across_factory_aliases():
    from sim.devices.dji.fixed import FixedRotorFactory

    class Watched(FixedRotorFactory):
        def __init__(self):
            self.created = 0
        def create(self, spec, context):
            self.created += 1
            return super().create(spec, context)
    options = {"motor_id": 1, "plant": "fixed-rotor", "position_rad": 0, "velocity_rad_s": 0}
    first, second = Watched(), Watched()
    registry = {"bus": CanBusFactory(), "one": first, "alias": first, "two": second}
    for kind in ("alias", "two"):
        topology = Topology((NodeSpec("bus", "bus"), NodeSpec("a", "one", options), NodeSpec("b", kind, options)),
                            (LinkSpec("a.can", "bus.can"), LinkSpec("b.can", "bus.can")))
        with pytest.raises(ValueError, match="duplicate M3508 motor_id 1 on bus bus"):
            Simulation(topology, registry)
    assert first.created == second.created == 0
    topology = Topology((NodeSpec("bus_a", "bus"), NodeSpec("bus_b", "bus"),
                         NodeSpec("a", "one", options), NodeSpec("b", "two", options)),
                        (LinkSpec("a.can", "bus_a.can"), LinkSpec("b.can", "bus_b.can")))
    with Simulation(topology, registry) as simulation:
        simulation.step()
        assert simulation.components["a"].device.motor_id == simulation.components["b"].device.motor_id == 1
    assert first.created == second.created == 1
