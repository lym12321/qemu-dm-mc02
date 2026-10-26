"""Explicit host CAN stimulus endpoint borrowing the existing CAN owner.

No transport, queue or clock lives here. Input frames take the active delivery
cut as their timestamp; any timestamp on a Python CanFrame is not a new clock.
"""

from collections.abc import Mapping

from sim.api import NodeDescription, PortSpec
from sim.fabric import CanFrame
from sim.fabric.factory import BorrowedCanPort


class CanHost:
    def __init__(self, context):
        self._context = context
        self._closed = False
        self._port = BorrowedCanPort(context)
        self._port.attach(self)

    def input(self, port, payload):
        if self._closed:
            raise RuntimeError("CAN host is closed")
        if port != "tx":
            raise ValueError("CAN host input requires tx")
        frame = CanFrame(payload.can_id, payload.flags, payload.dlc,
                         payload.data, self._context.t_sim_ns)
        self._port.send(self, frame)

    def receive(self, frame):
        if self._closed:
            raise RuntimeError("CAN host is closed")
        self._context.emit("can", "can-host.received", {"frame": frame},
                           source_time_ns=frame.timestamp_ns,
                           mapped_time_ns=frame.timestamp_ns)

    def close(self):
        self._closed = True


class CanHostFactory:
    @staticmethod
    def describe(spec):
        if spec.options:
            raise ValueError("CAN host has no options")
        return NodeDescription((PortSpec("can", "can", min_connections=1),
                                PortSpec("tx", "can-frame", "in", "signal",
                                         capabilities={"external_input"})),
                               frozenset({"input"}))

    @staticmethod
    def validate_input(spec, port, payload, *, connected_ports):
        CanHostFactory.describe(spec).port(port)
        if port != "tx":
            raise ValueError("CAN host input requires tx")
        if isinstance(payload, Mapping):
            if set(payload) - {"id", "flags", "dlc", "data_hex"} or not {"id", "dlc", "data_hex"} <= set(payload):
                raise ValueError("CAN input requires id, dlc, data_hex and optional flags")
            for key in ("id", "dlc"):
                if type(payload[key]) is not int:
                    raise ValueError(f"CAN {key} must be an integer")
            if type(payload.get("flags", 0)) is not int:
                raise ValueError("CAN flags must be an integer")
            raw = payload["data_hex"]
            if (not isinstance(raw, str) or len(raw) > 128 or len(raw) % 2
                    or any(char not in "0123456789abcdefABCDEF" for char in raw)):
                raise ValueError("CAN data_hex must be at most 64 complete hex bytes")
            payload = CanFrame(payload["id"], payload.get("flags", 0), payload["dlc"], bytes.fromhex(raw))
        if not isinstance(payload, CanFrame):
            raise ValueError("CAN input must be a CanFrame or a frame mapping")
        if any(type(value) is not int for value in (payload.can_id, payload.flags, payload.dlc, payload.timestamp_ns)):
            raise ValueError("CAN frame fields must be integers")
        # Re-admit even objects constructed through low-level dataclass tools.
        return CanFrame(payload.can_id, payload.flags, payload.dlc, payload.data)

    def validate_topology(self, topology, descriptions, registry):
        hosts = {node.name for node in topology.nodes if registry[node.kind] is self}
        if any(endpoint.node in hosts and endpoint.port == "tx"
               for link in topology.links for endpoint in (link.source, link.target)):
            raise ValueError("CAN host tx accepts external input only; connect its can port")

    @staticmethod
    def create(spec, context):
        return CanHost(context)
