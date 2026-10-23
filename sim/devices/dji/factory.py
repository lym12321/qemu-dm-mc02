"""Explicit fixed, rigid-rotor or shared native-joint M3508 execution."""

import math

from sim.api import NodeDescription, PortSpec
from sim.config.yaml import fields, parse_time, text_bytes


def _configuration(spec):
    options = dict(spec.options)
    joint = options.get("plant") == "joint"
    state_fields = {"joint"} if joint else {"position_rad", "velocity_rad_s"}
    fields(options, {"motor_id", "plant", "position_rad", "velocity_rad_s",
                     "joint", "command_timeout", "feedback_period", "temperature_c"},
           {"motor_id", "plant"} | state_fields, spec.name)
    if options["plant"] not in ("fixed-rotor", "rigid-rotor", "joint"):
        raise ValueError(f"{spec.name}: plant must be fixed-rotor, rigid-rotor or joint")
    if joint:
        if "position_rad" in options or "velocity_rad_s" in options:
            raise ValueError("joint motor initial state belongs to its world")
        text_bytes(options["joint"], "joint name")
        if "\0" in options["joint"]:
            raise ValueError("joint name must not contain NUL")
    elif "joint" in options:
        raise ValueError("joint name requires plant joint")
    motor_id = options["motor_id"]
    if type(motor_id) is not int or not 1 <= motor_id <= 8:
        raise ValueError("motor_id must be an integer in 1..8")
    for field in (() if joint else ("position_rad", "velocity_rad_s")):
        value = options[field]
        try:
            finite = type(value) in (float, int) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{field} must be finite")
    temperature = options.get("temperature_c", 25)
    if type(temperature) is not int or not 0 <= temperature <= 255:
        raise ValueError("temperature_c must be an integer in 0..255")
    result = {"motor_id": motor_id,
            "command_timeout_ns": parse_time(options.get("command_timeout", "100ms"),
                                             "command_timeout", zero=False),
            "feedback_period_ns": parse_time(options.get("feedback_period", "1ms"),
                                             "feedback_period", zero=False),
            "temperature_c": temperature}
    if not joint:
        result.update(position_rad=float(options["position_rad"]),
                      velocity_rad_s=float(options["velocity_rad_s"]))
    return result


class _BorrowedPort:
    """Forward admission to Simulation's connection; own no bus or queue."""

    def __init__(self, context):
        self.context = context
        self.endpoint = self.connection = None

    def attach(self, endpoint, name=None):
        if self.endpoint is not None:
            raise ValueError("M3508 already has an endpoint")
        self.endpoint = endpoint
        self.connection = self.context.attach("can", endpoint)

    def send(self, source, frame):
        if source is not self.endpoint:
            raise ValueError("unbound CAN source")
        self.connection.send(frame)


class FixedRotor:
    """Own one protocol device with configured, unchanging rotor state."""

    def __init__(self, spec, context):
        from .device import M3508Device
        options = _configuration(spec)
        self.position = options.pop("position_rad")
        self.velocity = options.pop("velocity_rad_s")
        self.device = M3508Device(_BorrowedPort(context), name=spec.name, **options)

    def reset(self, session):
        self.device.reset(session, position_rad=self.position, velocity_rad_s=self.velocity)

    def step(self, context):
        plan = self.device.prepare_step(context)
        return self.device.complete_step(plan, self.position, self.velocity)

    def close(self):
        self.device.close()


class _JointBuilder:
    def build(self, physics, bindings):
        from .joints import M3508JointExecution

        if len({id(binding.driver) for binding in bindings}) != len(bindings):
            raise ValueError("each M3508 device must have one joint binding")
        return M3508JointExecution(physics,
                                  {binding.driver: binding.joint for binding in bindings})


_JOINT_BUILDER = _JointBuilder()


class _JointMotor:
    """Own one device; the bound world exclusively resets and advances it."""

    def __init__(self, spec, context, options):
        from sim.physics.binding import JointDriveBinding
        from .device import M3508Device

        self.device = M3508Device(_BorrowedPort(context), name=spec.name, **options)
        try:
            self.binding = context.attach("joint", JointDriveBinding(
                spec.options["joint"], self.device, _JOINT_BUILDER))
        except BaseException as primary:
            try:
                self.device.close()
            except BaseException as error:
                primary.add_note(f"joint motor construction cleanup failed: {error}")
            raise

    def close(self):
        self.device.close()


class M3508Factory:
    def describe(self, spec):
        _configuration(spec)
        if spec.options["plant"] == "joint":
            return NodeDescription((
                PortSpec("can", "can", min_connections=1),
                PortSpec("joint", "joint-drive", category="binding", min_connections=1),
            ))
        return NodeDescription((PortSpec("can", "can", min_connections=1),), frozenset({"step"}))

    def describe_joint_binding(self, spec, port):
        _configuration(spec)
        if spec.options["plant"] != "joint" or port != "joint":
            raise ValueError("M3508 joint binding requires the joint motor port")
        return spec.options["joint"], _JOINT_BUILDER

    def create(self, spec, context):
        options = _configuration(spec)
        if spec.options["plant"] == "fixed-rotor":
            return FixedRotor(spec, context)
        if spec.options["plant"] == "joint":
            return _JointMotor(spec, context, options)
        from sim.physics import Rotor
        from .m3508 import M3508

        rotor = Rotor(initial_position=options.pop("position_rad"),
                      initial_velocity=options.pop("velocity_rad_s"))
        return M3508(_BorrowedPort(context), rotor=rotor, name=spec.name, **options)

    def validate_topology(self, topology, descriptions, registry):
        """C620 motor addresses must be unique on each connected bus."""
        from sim.api import Endpoint

        peers = {}
        for link in topology.links:
            peers[link.source] = link.target
            peers[link.target] = link.source
        identities = {}
        for node in topology.nodes:
            # Inspect every registered M3508 factory, including aliases
            # and separate instances; registry key spelling is not identity.
            if not isinstance(registry[node.kind], M3508Factory):
                continue
            peer = peers.get(Endpoint(node.name, "can"))
            if peer is None or "connection" not in descriptions[peer.node].capabilities:
                continue  # Ordinary link admission reports the invalid wiring.
            identity = (peer.node, node.options["motor_id"])
            previous = identities.get(identity)
            if previous is not None:
                raise ValueError(f"duplicate M3508 motor_id {identity[1]} on bus {peer.node}: {previous}, {node.name}")
            identities[identity] = node.name
