"""One shared native MuJoCo world under Simulation's execution ownership."""

from collections.abc import Mapping
import math
from pathlib import Path

from sim.api import (BackendUnavailable, ImuSignalSample, NodeDescription, PortSpec,
                     TimeQuality, validate_imu_sample)
from sim.config.yaml import fields, file_path, text_bytes

from .binding import JointDriveBinding
from .joints import JointExecution
from .participant import PhysicsParticipant


def _bindings(value):
    if not isinstance(value, Mapping):
        raise ValueError("MuJoCo joints must be a joint-to-actuator mapping")
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


def _imu_bindings(value):
    if not isinstance(value, Mapping):
        raise ValueError("MuJoCo imus must be an alias-to-sensor-pair mapping")
    result = {}
    for alias, pair in value.items():
        # Aliases form topology ports; native sensor names remain literal.
        if not isinstance(alias, str) or "." in alias:
            raise ValueError("MuJoCo IMU alias must be an identifier")
        PortSpec(f"imu.{alias}", "imu", "out", "signal")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"IMU {alias}: expected gyro and accelerometer names")
        for name in pair:
            text_bytes(name, f"IMU {alias} sensor name")
            if "\0" in name:
                raise ValueError(f"IMU {alias} sensor name must not contain NUL")
        result[alias] = tuple(pair)
    return result


def _configuration(spec):
    options = dict(spec.options)
    fields(options, {"model", "joints", "control", "imus"}, {"model"}, spec.name)
    if options.get("control", "direct") not in ("direct", "bound"):
        raise ValueError("MuJoCo control must be direct or bound")
    model = options["model"]
    if not isinstance(model, (str, Path)):
        raise ValueError("MuJoCo model must be a nonempty path")
    path = file_path(str(model), spec.base_dir, "MuJoCo model")
    joints, imus = _bindings(options.get("joints", {})), _imu_bindings(options.get("imus", {}))
    if not joints and not imus:
        raise ValueError("MuJoCo world requires joints or IMUs")
    if options.get("control", "direct") == "bound" and not joints:
        raise ValueError("bound MuJoCo world requires joints")
    return path, joints, imus


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


class _ImuOutput:
    def __init__(self, world, name):
        self._world, self._name = world, name

    def read_imu(self):
        return self._world._read_imu(self._name)


class _MujocoWorld:
    """Adapt existing joint control and sampling; add no solver or plant clock."""

    def __init__(self, physics, executor, context, joints, imus, *, bound=False):
        self._physics = physics
        self._executor = executor
        self._context = context
        self._joints = tuple(joints)
        self._imus = tuple(imus)
        self._bound = bound
        self._bindings = []
        self._closed = False
        self._completed_cut = None
        for name in self._imus:
            port = f"imu.{name}"
            if port in context.connected_ports:
                context.attach(port, _ImuOutput(self, name))

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
        if any(binding.driver is endpoint.driver for binding in self._bindings):
            raise ValueError("each driver must have one joint binding")
        self._bindings.append(endpoint)
        return endpoint

    def reset(self, session):
        self._completed_cut = None
        if self._bound and self._executor is None:
            if not self._bindings:
                raise ValueError("bound world requires joint drivers")
            self._executor = JointExecution(self._physics, tuple(self._bindings))
        self._executor.reset(session)
        self._completed_cut = session.t_sim_ns

    def step(self, context):
        self._completed_cut = None
        result = self._executor.step(context)
        if result.status == "ok" and result.reached_time_ns == context.t_sim_ns:
            self._completed_cut = context.t_sim_ns
        return result

    def _read_imu(self, name):
        """Pull completed native SI directly, even before the state event samples."""
        self._context.check_io()
        if self._closed or self._completed_cut is None:
            raise RuntimeError("IMU source requires a completed open world")
        return ImuSignalSample(self._physics.get_imu(name), None, self._completed_cut)

    def input(self, port, payload):
        if self._bound or not self._joints or port != "effort":
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
        if self._closed or cut != self._completed_cut:
            raise RuntimeError("world sample requires its completed cut")
        physics = self._physics
        payload = {
            "native_time_s": physics.time_s,
            "joints": {name: physics.get_joint_state(name) for name in self._joints},
        }
        if self._imus:
            samples = {name: physics.get_imu(name) for name in self._imus}
            for sample in samples.values():
                validate_imu_sample(sample)
            payload["imus"] = samples
        self._context.emit("state", "physics.state", payload,
                           source_time_ns=None, mapped_time_ns=cut,
                           delivery_time_ns=cut, quality=TimeQuality.SAMPLED)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._completed_cut = None
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
        _, joints, imus = _configuration(spec)
        imu_ports = tuple(PortSpec(f"imu.{name}", "imu", "out", "signal") for name in imus)
        if spec.options.get("control", "direct") == "bound":
            return NodeDescription((
                PortSpec("drives", "joint-drive", category="binding", max_connections=None,
                         min_connections=1),
                PortSpec("state", "joint-state", "out", "binding"),
            ) + imu_ports, {"binding_owner", "step", "sample"})
        ports = (PortSpec("state", "joint-state", "out", "binding"),) + imu_ports
        capabilities = {"step", "sample"}
        if joints:
            ports = (PortSpec("effort", "joint-effort", "in", "binding",
                              capabilities={"external_input"}),) + ports
            capabilities.add("input")
        return NodeDescription(ports, capabilities)

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        if (spec.options.get("control", "direct") != "direct" or not spec.options.get("joints")
                or port != "effort"):
            raise ValueError(f"MuJoCo port cannot accept external inputs: {port}")
        if port in connected_ports:
            raise ValueError(f"MuJoCo input is owned by a connection: {port}")
        return _efforts(spec.options["joints"], payload)

    def validate_topology(self, topology, descriptions, registry):
        specs = {node.name: node for node in topology.nodes}
        nodes = {node.name for node in topology.nodes
                 if isinstance(registry[node.kind], MujocoFactory)}
        occupied = set()
        for link in topology.links:
            if link.source.node not in nodes and link.target.node not in nodes:
                continue
            world, peer = ((link.source, link.target) if link.source.node in nodes
                           else (link.target, link.source))
            spec = specs[world.node]
            world_port = descriptions[world.node].port(world.port)
            if world_port.category == "signal" and world_port.protocol == "imu":
                continue  # The signal connection owns its endpoint admission.
            if (spec.options.get("control", "direct") != "bound" or world.port != "drives"
                    or peer.node in nodes):
                raise ValueError("MuJoCo connections require bound world drives")
            descriptor = descriptions[peer.node].port(peer.port)
            hook = getattr(registry[specs[peer.node].kind], "describe_joint_binding", None)
            if (descriptor.category != "binding" or descriptor.protocol != "joint-drive"
                    or descriptor.direction != "inout" or not callable(hook)):
                raise ValueError("world drives require a described joint-drive binding")
            joint = hook(specs[peer.node], peer.port)
            if not isinstance(joint, str) or joint not in spec.options["joints"]:
                raise ValueError(f"invalid world joint binding: {joint}")
            identity = (world.node, joint)
            if identity in occupied:
                raise ValueError("each world joint must have one driver")
            occupied.add(identity)

    def create(self, spec, context):
        path, joints, imus = _configuration(spec)
        try:
            import mujoco  # Native loading belongs only to execution.
        except (ImportError, OSError) as error:
            raise BackendUnavailable("MuJoCo backend is unavailable; install the mujoco extra") from error

        from .mujoco import MujocoPhysics

        physics = participant = None
        try:
            physics = MujocoPhysics.from_xml_path(path, joints=joints, imus=imus)
            bound = spec.options.get("control", "direct") == "bound"
            if not bound:
                participant = PhysicsParticipant(physics)
            return _MujocoWorld(physics, participant, context, joints, imus, bound=bound)
        except BaseException as primary:
            owner = participant if participant is not None else physics
            if owner is not None:
                try:
                    owner.close()
                except BaseException as error:
                    primary.add_note(f"MuJoCo construction cleanup failed: {error}")
            raise
