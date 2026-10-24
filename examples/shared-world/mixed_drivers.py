"""Explicit plugin example: an ideal torque driver and a C620 share one world.

Run this file after installing the mujoco extra. The ideal driver has no motor
or electrical model; native MuJoCo alone computes the joint motion.
"""

from dataclasses import asdict
import json
import math
from pathlib import Path

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.composition import builtin_registry
from sim.fabric import CanFrame
from sim.physics.binding import JointDriveBinding, JointDrivePlan
from sim.runtime import Simulation


class IdealEffortDriver:
    max_abs_effort_nm = 1.0

    def __init__(self, timeout_ns):
        self.timeout_ns = timeout_ns
        self._pending = None

    def validate_joint_state(self, state):
        if not all(type(value) in (int, float) and math.isfinite(value)
                   for value in (state.position_rad, state.velocity_rad_s)):
            raise ValueError("ideal driver requires finite joint state")

    def reset(self, session, initial_state):
        self.validate_joint_state(initial_state)
        self._session, self._step = session.session_id, 0
        self.t_sim_ns = session.t_sim_ns
        self._effort, self._expiry = 0.0, session.t_sim_ns
        self._pending, self.observation = None, initial_state

    def command(self, effort_nm, cut):
        if self._pending is not None or cut != self.t_sim_ns:
            raise RuntimeError("ideal command requires its completed cut")
        self._effort, self._expiry = effort_nm, cut + self.timeout_ns

    def prepare_step(self, context, start_state):
        if self._pending is not None:
            raise RuntimeError("ideal driver already has a prepared step")
        if (context.session_id != self._session or context.step_id != self._step + 1
                or context.t_sim_ns - context.dt_ns != self.t_sim_ns):
            raise ValueError("ideal driver requires its next exact step")
        self.validate_joint_state(start_state)
        expiry = max(self.t_sim_ns, min(context.t_sim_ns, self._expiry))
        self._pending = JointDrivePlan(context, expiry, self._effort)
        return self._pending

    def complete_step(self, plan, target_state):
        if plan is not self._pending or plan is None:
            raise ValueError("ideal completion requires its original prepared plan")
        self.validate_joint_state(target_state)
        self.observation = target_state
        self.t_sim_ns, self._step = plan.context.t_sim_ns, plan.context.step_id
        self._pending = None
        return StepResult("ok", self.t_sim_ns)


class _IdealEffort:
    def __init__(self, spec, context):
        self.context = context
        self.driver = IdealEffortDriver(spec.options["timeout_ns"])
        context.attach("joint", JointDriveBinding(spec.options["joint"], self.driver))
        self.closed = False

    def input(self, port, effort):
        self.context.check_io()
        self.driver.command(effort, self.context.t_sim_ns)

    def close(self):
        self.closed = True


class IdealEffortFactory:
    def describe(self, spec):
        if (set(spec.options) != {"joint", "timeout_ns"}
                or not isinstance(spec.options["joint"], str)
                or not spec.options["joint"] or "\0" in spec.options["joint"]
                or type(spec.options["timeout_ns"]) is not int
                or not 0 < spec.options["timeout_ns"] <= 0xFFFFFFFFFFFFFFFF):
            raise ValueError("ideal effort requires a joint and positive unsigned64 timeout_ns")
        return NodeDescription((
            PortSpec("joint", "joint-drive", category="binding", min_connections=1),
            PortSpec("effort", "joint-effort", "in", "binding",
                     capabilities={"external_input"}),
        ), {"input"})

    def describe_joint_binding(self, spec, port):
        self.describe(spec)
        if port != "joint":
            raise ValueError("ideal binding requires joint port")
        return spec.options["joint"]

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        if (port != "effort" or type(payload) not in (int, float)
                or not -1 <= payload <= 1):
            raise ValueError("ideal effort must be finite and within +/-1 Nm")
        return payload

    def create(self, spec, context):
        return _IdealEffort(spec, context)


class _CanCommand:
    def __init__(self, _spec, context):
        self.connection = context.attach("can", self)
        self.frames = []

    def start(self):
        self.connection.send(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000")))

    def receive(self, frame):
        self.frames.append(frame)

    def close(self):
        pass


class _CanCommandFactory:
    def describe(self, _spec):
        return NodeDescription((PortSpec("can", "can", min_connections=1),))

    def create(self, spec, context):
        return _CanCommand(spec, context)


def mixed_scene():
    registry = builtin_registry()
    # Register the plugin explicitly; the runtime and physics know no vendors.
    registry["example.ideal-effort"] = IdealEffortFactory()
    registry["example.can-command"] = _CanCommandFactory()
    topology = Topology((
        NodeSpec("world", "physics.mujoco", {
            "model": Path(__file__).with_name("world.xml"), "control": "bound",
            "joints": {"j1": "m1", "j2": "m2"}}),
        NodeSpec("can", "bus.can"), NodeSpec("command", "example.can-command"),
        NodeSpec("motor", "device.dji-m3508", {"plant": "joint", "motor_id": 1,
                 "joint": "j1", "gear_ratio": 2, "command_timeout": "3ms"}),
        NodeSpec("ideal", "example.ideal-effort", {"joint": "j2", "timeout_ns": 7_000_000}),
    ), (LinkSpec("command.can", "can.can"), LinkSpec("motor.can", "can.can"),
        LinkSpec("motor.joint", "world.drives"), LinkSpec("ideal.joint", "world.drives")))
    return topology, registry


def main():
    topology, registry = mixed_scene()
    with Simulation(topology, registry, quantum_ns=10_000_000) as simulation:
        simulation.schedule_input("ideal.effort", -.4)
        simulation.step()
        for event in simulation.events:
            if event.kind == "physics.state" and event.mapped_time_ns == 10_000_000:
                print(json.dumps({name: asdict(state)
                                  for name, state in event.payload["joints"].items()}))


if __name__ == "__main__":
    main()
