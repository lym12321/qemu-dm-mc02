"""One shared native MuJoCo world under Simulation's execution ownership."""

from collections.abc import Mapping
import math
from pathlib import Path

from sim.api import BackendUnavailable, NodeDescription, PortSpec, TimeQuality
from sim.config.yaml import fields, file_path, text_bytes

from .participant import PhysicsParticipant


def _bindings(value):
    if not isinstance(value, Mapping) or not value:
        raise ValueError("MuJoCo joints must be a nonempty joint-to-actuator mapping")
    result = {}
    for joint, actuator in value.items():
        for name, label in ((joint, "joint"), (actuator, "actuator")):
            text_bytes(name, f"MuJoCo {label} name")
            if "\0" in name:
                raise ValueError(f"MuJoCo {label} name must not contain NUL")
        result[joint] = actuator
    if len(set(result.values())) != len(result):
        raise ValueError("MuJoCo actuator bindings must be distinct")
    return result


def _configuration(spec):
    options = dict(spec.options)
    fields(options, {"model", "joints"}, {"model", "joints"}, spec.name)
    model = options["model"]
    if not isinstance(model, (str, Path)):
        raise ValueError("MuJoCo model must be a nonempty path")
    path = file_path(str(model), spec.base_dir, "MuJoCo model")
    return path, _bindings(options["joints"])


def _efforts(joints, payload):
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("joint effort must be a nonempty joint-to-torque mapping")
    result = {}
    for name, value in payload.items():
        if not isinstance(name, str) or name not in joints:
            raise ValueError(f"unbound joint: {name}")
        if type(value) not in (int, float):
            raise ValueError("joint effort must be a finite plain number")
        try:
            effort = float(value)
        except OverflowError as error:
            raise ValueError("joint effort must be finite") from error
        if not math.isfinite(effort):
            raise ValueError("joint effort must be finite")
        result[name] = effort
    return result


class _MujocoWorld:
    """Adapt existing joint control and sampling; add no solver or plant clock."""

    def __init__(self, participant, context, joints):
        self._participant = participant
        self._context = context
        self._joints = tuple(joints)

    def reset(self, session):
        self._participant.reset(session)

    def step(self, context):
        return self._participant.step(context)

    def input(self, port, payload):
        if port != "effort":
            raise ValueError(f"MuJoCo port cannot accept inputs: {port}")
        efforts = _efforts(self._joints, payload)
        physics = self._participant.physics
        # Native admission of every held control precedes all writes. A later
        # setter/solver failure remains terminal in Simulation; never roll back.
        for name, value in efforts.items():
            physics.validate_joint_effort(name, value)
        for name, value in efforts.items():
            physics.set_joint_effort(name, value)

    def sample(self, cut):
        physics = self._participant.physics
        payload = {
            "native_time_s": physics.time_s,
            "joints": {name: physics.get_joint_state(name) for name in self._joints},
        }
        self._context.emit("state", "physics.state", payload,
                           source_time_ns=None, mapped_time_ns=cut,
                           delivery_time_ns=cut, quality=TimeQuality.SAMPLED)

    def close(self):
        self._participant.close()


class MujocoFactory:
    """Pure topology/input admission with native creation deferred to execution."""

    def describe(self, spec):
        _configuration(spec)
        return NodeDescription((
            PortSpec("effort", "joint-effort", "in", "binding",
                     capabilities={"external_input"}),
            PortSpec("state", "joint-state", "out", "binding"),
        ), {"step", "input", "sample"})

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        if port != "effort":
            raise ValueError(f"MuJoCo port cannot accept external inputs: {port}")
        if port in connected_ports:
            raise ValueError(f"MuJoCo input is owned by a connection: {port}")
        return _efforts(spec.options["joints"], payload)

    def validate_topology(self, topology, descriptions, registry):
        nodes = {node.name for node in topology.nodes
                 if isinstance(registry[node.kind], MujocoFactory)}
        for link in topology.links:
            if link.source.node in nodes or link.target.node in nodes:
                raise ValueError("MuJoCo joint bindings do not yet support connections")

    def create(self, spec, context):
        path, joints = _configuration(spec)
        try:
            import mujoco  # Native loading belongs only to execution.
        except (ImportError, OSError) as error:
            raise BackendUnavailable("MuJoCo backend is unavailable; install the mujoco extra") from error

        from .mujoco import MujocoPhysics

        physics = participant = None
        try:
            physics = MujocoPhysics.from_xml_path(path, joints=joints)
            participant = PhysicsParticipant(physics)
            return _MujocoWorld(participant, context, joints)
        except BaseException as primary:
            owner = participant if participant is not None else physics
            if owner is not None:
                try:
                    owner.close()
                except BaseException as error:
                    primary.add_note(f"MuJoCo construction cleanup failed: {error}")
            raise
