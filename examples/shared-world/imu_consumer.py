"""A custom SI consumer connected through the public Simulation topology.

Run after installing the project with its mujoco extra. This example receives
completed samples; it implements no physics, sensor model or execution loop.
"""

from dataclasses import asdict
import json
from pathlib import Path

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.runtime import Simulation


class Monitor:
    def __init__(self, _spec, context):
        context.attach("imu", self)

    def receive_imu(self, sample):
        print(json.dumps(asdict(sample)))

    def close(self):
        pass  # The connection and native world have their own owners.


class MonitorFactory:
    def describe(self, _spec):
        return NodeDescription((PortSpec("imu", "imu", "in", "signal",
                                         min_connections=1),))

    def create(self, spec, context):
        return Monitor(spec, context)


def main():
    registry = builtin_registry()
    registry["example.imu-monitor"] = MonitorFactory()
    scene = load_scene(Path(__file__).with_name("imu.yaml"), registry)
    topology = Topology((*scene.topology.nodes, NodeSpec("imu_wire", "net.imu"),
                         NodeSpec("monitor", "example.imu-monitor")),
                        (LinkSpec("world.imu.body", "imu_wire.signal"),
                         LinkSpec("imu_wire.signal", "monitor.imu")))
    with Simulation(topology, registry, quantum_ns=scene.quantum_ns) as simulation:
        simulation.schedule_input("world.effort", {"shaft": .4}, at_ns=0)
        simulation.schedule_input("world.effort", {"shaft": 0}, at_ns=10_000_000)
        simulation.run_until(scene.duration_ns)


if __name__ == "__main__":
    main()
