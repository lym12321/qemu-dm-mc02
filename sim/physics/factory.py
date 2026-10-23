"""One shared native MuJoCo world under Simulation's execution ownership."""

from collections.abc import Mapping
import math
from pathlib import Path

from sim.api import BackendUnavailable, NodeDescription, PortSpec, TimeQuality
from sim.config.yaml import fields, file_path, text_bytes

from .binding import JointDriveBinding
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
    fields(options, {"model", "joints", "control"}, {"model", "joints"}, spec.name)
    if options.get("control", "direct") not in ("direct", "bound"):
        raise ValueError("MuJoCo control must be direct or bound")
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

    def __init__(self, physics, executor, context, joints, *, bound=False):
        self._physics = physics
        self._executor = executor
        self._context = context
        self._joints = tuple(joints)
        self._bound = bound
        self._bindings = []
        self._closed = False

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        if (not self._bound or self._executor is not None or self._closed
                or owner_port != "drives" or descriptor.category != "binding"
                or descriptor.protocol != "joint-drive" or descriptor.direction != "inout"):
            raise ValueError("world requires an uninitialized joint-drive binding")
        if not isinstance(endpoint, JointDriveBinding):
            raise TypeError("world requires JointDriveBinding")
        if endpoint.joint not in self._joints:
            raise ValueError(f"unbound joint: {endpoint.joint}")
        if any(binding.joint == endpoint.joint for binding in self._bindings):
            raise ValueError("each world joint must have one driver")
        if self._bindings and endpoint.builder is not self._bindings[0].builder:
            raise ValueError("world requires one joint driver family")
        self._bindings.append(endpoint)
        return endpoint

    def reset(self, session):
        if self._bound and self._executor is None:
            if not self._bindings:
                raise ValueError("bound world requires joint drivers")
            self._executor = self._bindings[0].builder.build(self._physics, tuple(self._bindings))
            if any(not callable(getattr(self._executor, method, None))
                   for method in ("reset", "step", "close")):
                raise TypeError("joint executor requires reset, step and close")
        self._executor.reset(session)

    def step(self, context):
        return self._executor.step(context)

    def input(self, port, payload):
        if self._bound or port != "effort":
            raise ValueError(f"MuJoCo port cannot accept inputs: {port}")
        efforts = _efforts(self._joints, payload)
        physics = self._physics
        # Native admission of every held control precedes all writes. A later
        # setter/solver failure remains terminal in Simulation; never roll back.
        for name, value in efforts.items():
            physics.validate_joint_effort(name, value)
        for name, value in efforts.items():
            physics.set_joint_effort(name, value)

    def sample(self, cut):
        physics = self._physics
        payload = {
            "native_time_s": physics.time_s,
            "joints": {name: physics.get_joint_state(name) for name in self._joints},
        }
        self._context.emit("state", "physics.state", payload,
                           source_time_ns=None, mapped_time_ns=cut,
                           delivery_time_ns=cut, quality=TimeQuality.SAMPLED)

    def close(self):
        if self._closed:
            return
        self._closed = True
        primary = None
        if self._executor is not None:
            try:
                self._executor.close()
            except BaseException as error:
                primary = error
        if self._bound:
            try:
                self._physics.close()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note(f"joint physics close failed: {error}")
        if primary is not None:
            raise primary


class MujocoFactory:
    """Pure topology/input admission with native creation deferred to execution."""

    def describe(self, spec):
        _configuration(spec)
        if spec.options.get("control", "direct") == "bound":
            return NodeDescription((
                PortSpec("drives", "joint-drive", category="binding", max_connections=None,
                         min_connections=1),
                PortSpec("state", "joint-state", "out", "binding"),
            ), {"binding_owner", "step", "sample"})
        return NodeDescription((
            PortSpec("effort", "joint-effort", "in", "binding",
                     capabilities={"external_input"}),
            PortSpec("state", "joint-state", "out", "binding"),
        ), {"step", "input", "sample"})

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        if spec.options.get("control", "direct") != "direct" or port != "effort":
            raise ValueError(f"MuJoCo port cannot accept external inputs: {port}")
        if port in connected_ports:
            raise ValueError(f"MuJoCo input is owned by a connection: {port}")
        return _efforts(spec.options["joints"], payload)

    def validate_topology(self, topology, descriptions, registry):
        specs = {node.name: node for node in topology.nodes}
        nodes = {node.name for node in topology.nodes
                 if isinstance(registry[node.kind], MujocoFactory)}
        occupied, families = set(), {}
        for link in topology.links:
            if link.source.node not in nodes and link.target.node not in nodes:
                continue
            world, peer = ((link.source, link.target) if link.source.node in nodes
                           else (link.target, link.source))
            spec = specs[world.node]
            if (spec.options.get("control", "direct") != "bound" or world.port != "drives"
                    or peer.node in nodes):
                raise ValueError("MuJoCo connections require bound world drives")
            descriptor = descriptions[peer.node].port(peer.port)
            hook = getattr(registry[specs[peer.node].kind], "describe_joint_binding", None)
            if (descriptor.category != "binding" or descriptor.protocol != "joint-drive"
                    or descriptor.direction != "inout" or not callable(hook)):
                raise ValueError("world drives require a described joint-drive binding")
            joint, builder = hook(specs[peer.node], peer.port)
            if (not isinstance(joint, str) or joint not in spec.options["joints"]
                    or not callable(getattr(builder, "build", None))):
                raise ValueError(f"invalid world joint binding: {joint}")
            identity = (world.node, joint)
            if identity in occupied:
                raise ValueError("each world joint must have one driver")
            occupied.add(identity)
            if world.node in families and families[world.node] is not builder:
                raise ValueError("world requires one joint driver family")
            families[world.node] = builder

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
            bound = spec.options.get("control", "direct") == "bound"
            if not bound:
                participant = PhysicsParticipant(physics)
            return _MujocoWorld(physics, participant, context, joints, bound=bound)
        except BaseException as primary:
            owner = participant if participant is not None else physics
            if owner is not None:
                try:
                    owner.close()
                except BaseException as error:
                    primary.add_note(f"MuJoCo construction cleanup failed: {error}")
            raise
