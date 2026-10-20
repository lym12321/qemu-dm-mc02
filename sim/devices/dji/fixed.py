"""Explicit fixed rotor observations over the existing C620 protocol owner."""

import math

from sim.api import NodeDescription, PortSpec
from sim.config.yaml import fields, parse_time


def _configuration(spec):
    options = dict(spec.options)
    fields(options, {"motor_id", "plant", "position_rad", "velocity_rad_s",
                     "command_timeout", "feedback_period", "temperature_c"},
           {"motor_id", "plant", "position_rad", "velocity_rad_s"}, spec.name)
    if options["plant"] != "fixed-rotor":
        raise ValueError(f"{spec.name}: this factory requires explicit plant: fixed-rotor")
    motor_id = options["motor_id"]
    if type(motor_id) is not int or not 1 <= motor_id <= 8:
        raise ValueError("motor_id must be an integer in 1..8")
    for field in ("position_rad", "velocity_rad_s"):
        value = options[field]
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
    temperature = options.get("temperature_c", 25)
    if type(temperature) is not int or not 0 <= temperature <= 255:
        raise ValueError("temperature_c must be an integer in 0..255")
    return {"motor_id": motor_id, "position_rad": float(options["position_rad"]),
            "velocity_rad_s": float(options["velocity_rad_s"]),
            "command_timeout_ns": parse_time(options.get("command_timeout", "100ms"),
                                             "command_timeout", zero=False),
            "feedback_period_ns": parse_time(options.get("feedback_period", "1ms"),
                                             "feedback_period", zero=False),
            "temperature_c": temperature}


class _BorrowedPort:
    """Forward admission to Simulation's connection; own no bus or queue."""

    def __init__(self, context):
        self.context = context
        self.endpoint = self.connection = None

    def attach(self, endpoint, name=None):
        if self.endpoint is not None:
            raise ValueError("fixed rotor already has an endpoint")
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


class FixedRotorFactory:
    def describe(self, spec):
        _configuration(spec)
        return NodeDescription((PortSpec("can", "can", min_connections=1),), frozenset({"step"}))

    def create(self, spec, context):
        return FixedRotor(spec, context)

    def validate_topology(self, topology, descriptions, registry):
        """C620 motor addresses must be unique on each connected bus."""
        from sim.api import Endpoint

        peers = {}
        for link in topology.links:
            peers[link.source] = link.target
            peers[link.target] = link.source
        identities = {}
        for node in topology.nodes:
            # Inspect every registered fixed-rotor factory, including aliases
            # and separate instances; registry key spelling is not identity.
            if not isinstance(registry[node.kind], FixedRotorFactory):
                continue
            peer = peers.get(Endpoint(node.name, "can"))
            if peer is None or "connection" not in descriptions[peer.node].capabilities:
                continue  # Ordinary link admission reports the invalid wiring.
            identity = (peer.node, node.options["motor_id"])
            previous = identities.get(identity)
            if previous is not None:
                raise ValueError(f"duplicate M3508 motor_id {identity[1]} on bus {peer.node}: {previous}, {node.name}")
            identities[identity] = node.name
