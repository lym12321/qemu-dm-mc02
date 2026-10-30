"""Test graph assembly using the production Simulation, with no step wrapper."""

from types import SimpleNamespace

from sim.api import NodeDescription, NodeSpec, Session, Topology
from sim.fabric.runtime import BorrowedCanDispatcher
from sim.runtime import Simulation


def simulation_for(owners, *, buses=(), session=None):
    """Take ownership of supplied components and borrow prewired CAN buses.

    Each caller supplies fresh owners for a new lifecycle. Simulation retains
    all scheduling, time, failure and cleanup behavior; this helper only builds
    explicit test factories and nodes. Bus dispatchers reset before components.
    """
    nodes, registry = [], {}
    for index, bus in enumerate(buses):
        name = f"bus_{index}"
        nodes.append(NodeSpec(name, name))
        registry[name] = SimpleNamespace(
            describe=lambda spec: NodeDescription(capabilities={"dispatch"}),
            create=lambda spec, context, bus=bus: BorrowedCanDispatcher(bus))
    for name, owner in owners.items():
        nodes.append(NodeSpec(name, name))
        registry[name] = SimpleNamespace(
            describe=lambda spec: NodeDescription(capabilities={"step"}),
            create=lambda spec, context, owner=owner: owner)
    return Simulation(Topology(tuple(nodes)), registry,
                      initial_session=session or Session(1), quantum_ns=(1 << 64) - 1)
