"""Explicit CAN worker recipes; Simulation owns every acquired session graph.

The native worker owns transport framing and RESET handshakes. These recipes
borrow its stream/mapper without acquiring a socket or advancing another clock.
"""

from sim.api import LinkSpec, NodeSpec, Topology
from sim.devices.dji.factory import M3508Factory
from sim.fabric.factory import CanBusFactory
from sim.machines.qemu.bridge import CanTransportBridgeFactory
from sim.runtime import Simulation


def motor_nodes(ids, *, command_timeout_ns=100_000_000, torque_per_amp=.02,
                joints=None):
    ids = tuple(ids)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("motor IDs must be nonempty and unique")
    if type(command_timeout_ns) is not int or not 0 < command_timeout_ns <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("command_timeout_ns must be a positive unsigned64 integer")
    nodes = []
    factory = M3508Factory()
    for motor_id in ids:
        options = dict(motor_id=motor_id, command_timeout=f"{command_timeout_ns}ns",
                       torque_per_amp=torque_per_amp)
        if joints is None:
            options.update(plant="rigid-rotor", position_rad=0, velocity_rad_s=0)
        else:
            options.update(plant="joint", joint=joints[motor_id - 1])
        spec = NodeSpec(f"motor{motor_id}", "device.dji-m3508", options)
        factory.describe(spec)
        nodes.append(spec)
    return tuple(nodes)


def can_topology(motors, *, delay_ns=0, socketcan=False, world=None):
    bus = NodeSpec("can", "bus.can", {"delay_ns": delay_ns})
    CanBusFactory().describe(bus)
    nodes = (bus, NodeSpec("transport", "host.can"), *motors)
    links = [LinkSpec("transport.qemu", "can.can")]
    if socketcan:
        links.append(LinkSpec("transport.socketcan", "can.can"))
    for motor in motors:
        links.append(LinkSpec(f"{motor.name}.can", "can.can"))
        if world is not None:
            links.append(LinkSpec(f"{motor.name}.joint", "world.drives"))
    if world is not None:
        nodes += (world,)
    return Topology(nodes, tuple(links))


def create_can_simulation(session, mapper, stream, datagrams, *, topology,
                          quantum_ns=1_000_000, on_event=None, drop=None,
                          on_delivery=None, extra_factories=None):
    registry = {
        "bus.can": CanBusFactory(drop=drop, on_delivery=on_delivery),
        "host.can": CanTransportBridgeFactory(stream, mapper, datagrams),
        "device.dji-m3508": M3508Factory(),
    }
    if any(node.kind == "physics.mujoco" for node in topology.nodes):
        from sim.physics.factory import MujocoFactory
        registry["physics.mujoco"] = MujocoFactory()
    if extra_factories is not None:
        overlap = registry.keys() & extra_factories.keys()
        if overlap:
            raise ValueError(f"duplicate CAN worker factories: {sorted(overlap)}")
        registry.update(extra_factories)
    return Simulation(topology, registry, initial_session=session,
                      quantum_ns=quantum_ns, on_event=on_event)


def rotor_simulation_factory(ids=(1,), *, delay_ns=0,
                             command_timeout_ns=100_000_000,
                             torque_per_amp=.02):
    """Pure profile admission before the worker opens any transport."""
    motors = motor_nodes(ids, command_timeout_ns=command_timeout_ns,
                         torque_per_amp=torque_per_amp)
    can_topology(motors, delay_ns=delay_ns)

    def acquire(session, mapper, stream, datagrams):
        topology = can_topology(motors, delay_ns=delay_ns,
                                socketcan=datagrams is not None)
        return create_can_simulation(session, mapper, stream, datagrams,
                                     topology=topology)
    return acquire
