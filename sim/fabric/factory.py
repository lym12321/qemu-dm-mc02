"""Explicit CAN factory; description and option admission are resource free."""

from sim.api import NodeDescription, PortSpec

from .can import CanBus, check_can_time


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

    def __init__(self, spec, context):
        self._sources = {}
        self.bus = CanBus(**spec.options, log=self._log)

    @property
    def dispatch_budget(self):
        return self.bus.dispatch_budget

    @property
    def next_due_ns(self):
        return self.bus.next_due_ns

    @property
    def pending(self):
        return self.bus.pending

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        if (owner_port != "can" or descriptor.category != "protocol"
                or descriptor.protocol != "can" or descriptor.direction != "inout"):
            raise ValueError("CAN connections require inout CAN protocol ports")
        self.bus.attach(endpoint, endpoint_name)
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

    def reset(self, session):
        self.bus.reset(session.t_sim_ns)

    def dispatch(self, cut, budget):
        return self.bus.step(cut, max_dispatch=budget)

    def close(self):
        self.bus.close()


class CanBusFactory:
    @staticmethod
    def describe(spec):
        allowed = {"delay_ns", "loopback", "max_pending", "dispatch_budget"}
        unknown = set(spec.options) - allowed
        if unknown:
            raise ValueError(f"unknown CAN bus options: {', '.join(sorted(unknown))}")
        check_can_time(spec.options.get("delay_ns", 0))
        if type(spec.options.get("loopback", False)) is not bool:
            raise ValueError("CAN loopback must be a boolean")
        for key in ("max_pending", "dispatch_budget"):
            value = spec.options.get(key, 256)
            if type(value) is not int or not 0 < value <= 65536:
                raise ValueError(f"CAN {key} must be an integer in 1..65536")
        return NodeDescription((PortSpec("can", "can", max_connections=None),),
                               frozenset({"connection", "dispatch"}))

    def validate_topology(self, topology, descriptions, registry):
        owners = {node.name for node in topology.nodes
                  if isinstance(registry[node.kind], CanBusFactory)}
        for link in topology.links:
            if link.source.node not in owners and link.target.node not in owners:
                continue
            for endpoint in (link.source, link.target):
                descriptor = descriptions[endpoint.node].port(endpoint.port)
                if (descriptor.category != "protocol" or descriptor.protocol != "can"
                        or descriptor.direction != "inout"):
                    raise ValueError("CAN connections require inout CAN protocol ports")

    @staticmethod
    def create(spec, context):
        return CanNetwork(spec, context)
