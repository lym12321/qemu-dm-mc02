"""Explicit CAN factory; description and option admission are resource free."""

from collections.abc import Mapping
import re

from sim.api import Endpoint, NodeDescription, PortSpec
from sim.api.topology import immutable

from .can import CanBus, check_can_time


_PROFILE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")


def _fault_profiles(spec):
    """Resolve bounded declarative policies without touching a bus or peer."""
    base_delay = spec.options.get("delay_ns", 0)
    check_can_time(base_delay)
    profiles = spec.options.get("fault_profiles", {})
    if not isinstance(profiles, Mapping) or len(profiles) > 16:
        raise ValueError("CAN fault_profiles must be a mapping of at most 16 profiles")
    resolved = {"normal": {"delay_ns": base_delay, "drop": ()}}
    rule_count = 0
    for name, profile in profiles.items():
        if (type(name) is not str or not _PROFILE_NAME.fullmatch(name)
                or name == "normal"):
            raise ValueError("CAN fault profile name must be an ASCII identifier of at most 64 characters; normal is reserved")
        if not isinstance(profile, Mapping) or set(profile) - {"delay_ns", "drop"}:
            raise ValueError("CAN fault profile accepts only delay_ns and drop")
        delay = profile.get("delay_ns", base_delay)
        check_can_time(delay)
        rules = profile.get("drop", ())
        if not isinstance(rules, (tuple, list)):
            raise ValueError("CAN fault drop must be a list of rules")
        rule_count += len(rules)
        if rule_count > 64:
            raise ValueError("CAN fault profiles permit at most 64 drop rules total")
        for rule in rules:
            if (not isinstance(rule, Mapping) or not rule
                    or set(rule) - {"source", "destination", "can_id"}):
                raise ValueError("CAN drop rule requires source, destination or can_id only")
            for key in ("source", "destination"):
                if key in rule:
                    if type(rule[key]) is not str:
                        raise ValueError(f"CAN drop {key} must be a qualified endpoint string")
                    Endpoint.parse(rule[key])
            if "can_id" in rule and (type(rule["can_id"]) is not int
                                      or not 0 <= rule["can_id"] <= 0x1fffffff):
                raise ValueError("CAN drop can_id must be an integer in 0..0x1fffffff")
        resolved[name] = {"delay_ns": delay, "drop": rules}
    return immutable(resolved)


class BorrowedCanPort:
    """Adapt one protocol endpoint to an admitted Simulation connection."""

    def __init__(self, context, port="can"):
        self._context = context
        self._port = port
        self._endpoint = self._connection = None

    def attach(self, endpoint, name=None):
        if self._endpoint is not None:
            raise ValueError("CAN port already has an endpoint")
        connection = self._context.attach(self._port, endpoint)
        self._endpoint, self._connection = endpoint, connection

    def send(self, source, frame):
        if source is not self._endpoint or self._connection is None:
            raise ValueError("unbound CAN source")
        self._connection.send(frame)


class CanConnection:
    """Borrow one CAN endpoint; every send obeys the active cut floor."""

    def __init__(self, context, port, bus, endpoint):
        self._context = context
        self.port = port
        self._bus = bus
        self._endpoint = endpoint

    def send(self, frame):
        self._context.check_io()
        self._bus.send(self._endpoint, frame, not_before_ns=self._context.not_before_ns)
        self._context.emit(self.port, "can.sent", {"frame": frame},
                           source_time_ns=frame.timestamp_ns,
                           mapped_time_ns=frame.timestamp_ns)


class CanNetwork:
    """Own the existing sole CAN queue, adapting the generic dispatch contract."""

    def __init__(self, spec, context, *, drop=None, on_delivery=None):
        self._context = context
        self._sources = {}
        self._endpoint_names = {}
        self._on_delivery = on_delivery
        self._inherited_drop = drop
        self._profiles = _fault_profiles(spec)
        self._profile = "normal"
        self._closed = self._dispatching = self._applying = False
        self._failed = None
        options = {key: value for key, value in spec.options.items()
                   if key != "fault_profiles"}
        self.bus = CanBus(**options, drop=self._drop, log=self._log)

    def _check_failed(self):
        if self._failed is not None:
            raise self._failed

    def _freeze(self, error):
        if self._failed is None:
            self._failed = error
        return self._failed

    def _check_mutation(self):
        if self._closed:
            raise RuntimeError("CAN network is closed")
        if self._dispatching or self._applying:
            raise self._freeze(RuntimeError("CAN network policy/lifecycle cannot change from a callback"))

    def _drop(self, source, destination, frame):
        # Do not short-circuit the borrowed callback: it may observe each peer
        # or raise its own primary error even when a declared rule matches.
        inherited = (bool(self._inherited_drop(source, destination, frame))
                     if self._inherited_drop is not None else False)
        self._check_failed()
        rules = self._profiles[self._profile]["drop"]
        if not rules:
            return inherited
        names = {}
        for key, endpoint in (("source", source), ("destination", destination)):
            attached, name = self._endpoint_names[id(endpoint)]
            if attached is not endpoint:
                raise ValueError("CAN fault endpoint is not attached")
            names[key] = name
        return inherited or any(all(
            frame.can_id == value if key == "can_id" else names[key] == value
            for key, value in rule.items()) for rule in rules)

    def input(self, port, payload):
        self._check_mutation()
        self._check_failed()
        self._context.check_io()
        if (port != "fault" or len(self._profiles) == 1
                or type(payload) is not str or payload not in self._profiles):
            raise ValueError("CAN fault input requires a configured profile name or normal")
        policy = self._profiles[payload]
        previous = self._profile
        self._applying = True
        try:
            self.bus.delay_ns = policy["delay_ns"]
            self._profile = payload
            cut = self._context.t_sim_ns
            self._context.emit("fault", "can.fault.applied", {
                "previous_profile": previous, "profile": payload,
                "delay_ns": policy["delay_ns"], "drop": policy["drop"], "cut_ns": cut,
            }, source_time_ns=cut, mapped_time_ns=cut, delivery_time_ns=cut)
            self._check_failed()
        except BaseException as error:
            raise self._freeze(error)
        finally:
            self._applying = False

    @property
    def dispatch_budget(self):
        return self.bus.dispatch_budget

    @property
    def next_due_ns(self):
        if not self._closed:
            self._check_failed()
        return self.bus.next_due_ns

    @property
    def pending(self):
        return self.bus.pending

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        if (owner_port != "can" or descriptor.category != "protocol"
                or descriptor.protocol != "can" or descriptor.direction != "inout"):
            raise ValueError("CAN connections require inout CAN protocol ports")
        self.bus.attach(endpoint, endpoint_name)
        self._endpoint_names[id(endpoint)] = (endpoint, endpoint_name)
        self._sources[endpoint_name] = (context, descriptor.name)
        return CanConnection(context, descriptor.name, self.bus, endpoint)

    def _log(self, delivery):
        context, port = self._sources[delivery.source]
        context.emit(port, "can.delivery", {
            "destination": delivery.destination, "frame": delivery.frame,
            "bus_sequence": delivery.sequence, "due_ns": delivery.due_ns,
            "late": delivery.late, "dropped": delivery.dropped,
        }, source_time_ns=delivery.timestamp_ns,
            mapped_time_ns=delivery.timestamp_ns,
            delivery_time_ns=delivery.delivered_ns)
        self._check_failed()
        if self._on_delivery is not None:
            self._on_delivery(delivery)
            context.check_deadline()
        self._check_failed()

    def reset(self, session):
        self._check_mutation()
        self.bus.reset(session.t_sim_ns)
        self.bus.delay_ns = self._profiles["normal"]["delay_ns"]
        self._profile = "normal"
        self._failed = None

    def dispatch(self, cut, budget):
        if self._closed:
            raise RuntimeError("CAN network is closed")
        self._check_failed()
        if self._dispatching or self._applying:
            raise self._freeze(RuntimeError("CAN network dispatch cannot reenter"))
        self._dispatching = True
        try:
            result = self.bus.step(cut, max_dispatch=budget)
            self._check_failed()
            return result
        except BaseException as error:
            raise self._freeze(error)
        finally:
            self._dispatching = False

    def close(self):
        if self._closed:
            return
        self._check_mutation()
        self.bus.close()
        self._closed = True
        self._endpoint_names.clear()
        self._sources.clear()


class CanBusFactory:
    def __init__(self, *, drop=None, on_delivery=None):
        for name, callback in (("drop", drop), ("on_delivery", on_delivery)):
            if callback is not None and not callable(callback):
                raise TypeError(f"CAN {name} must be callable or None")
        self._drop, self._on_delivery = drop, on_delivery

    @staticmethod
    def describe(spec):
        allowed = {"delay_ns", "loopback", "max_pending", "dispatch_budget", "fault_profiles"}
        unknown = set(spec.options) - allowed
        if unknown:
            raise ValueError(f"unknown CAN bus options: {', '.join(sorted(unknown))}")
        profiles = _fault_profiles(spec)
        if type(spec.options.get("loopback", False)) is not bool:
            raise ValueError("CAN loopback must be a boolean")
        for key in ("max_pending", "dispatch_budget"):
            value = spec.options.get(key, 256)
            if type(value) is not int or not 0 < value <= 65536:
                raise ValueError(f"CAN {key} must be an integer in 1..65536")
        ports = (PortSpec("can", "can", max_connections=None),)
        capabilities = {"connection", "dispatch"}
        if len(profiles) > 1:
            ports += (PortSpec("fault", "can-fault", "in", "signal",
                               capabilities={"external_input"}),)
            capabilities.add("input")
        return NodeDescription(ports, frozenset(capabilities))

    @staticmethod
    def validate_input(spec, port, payload, *, connected_ports):
        CanBusFactory.describe(spec).port(port)
        if port != "fault" or "fault" in connected_ports:
            raise ValueError("CAN fault accepts external input only")
        if type(payload) is not str or payload not in _fault_profiles(spec):
            raise ValueError("CAN fault input requires a configured profile name or normal")
        return payload

    def validate_topology(self, topology, descriptions, registry):
        owners = {node.name: node for node in topology.nodes
                  if isinstance(registry[node.kind], CanBusFactory)}
        members = {name: set() for name in owners}
        for link in topology.links:
            if link.source.node not in owners and link.target.node not in owners:
                continue
            if any(endpoint.node in owners and endpoint.port == "fault"
                   for endpoint in (link.source, link.target)):
                raise ValueError("CAN fault accepts external input only; connect its can port")
            for endpoint in (link.source, link.target):
                descriptor = descriptions[endpoint.node].port(endpoint.port)
                if (descriptor.category != "protocol" or descriptor.protocol != "can"
                        or descriptor.direction != "inout"):
                    raise ValueError("CAN connections require inout CAN protocol ports")
            for owner, peer in ((link.source, link.target), (link.target, link.source)):
                if owner.node in owners:
                    members[owner.node].add(str(peer))
        for name, spec in owners.items():
            for policy in _fault_profiles(spec).values():
                for rule in policy["drop"]:
                    for key in ("source", "destination"):
                        if key in rule and rule[key] not in members[name]:
                            raise ValueError(f"CAN drop {key} {rule[key]} is not attached to bus {name}")

    def create(self, spec, context):
        return CanNetwork(spec, context, drop=self._drop,
                          on_delivery=self._on_delivery)
