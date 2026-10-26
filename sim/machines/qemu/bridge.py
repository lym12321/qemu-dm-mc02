"""Session-local CAN endpoints over borrowed worker transports.

Simulation owns the CAN graph and its delivery cuts. The caller retains the
streams, sockets and queued physical byte suffixes across graph replacement.
"""

from sim.api import NodeDescription, PortSpec
from sim.fabric.can import check_can_time
from sim.fabric.factory import BorrowedCanPort
from sim.fabric.frame import CanFrame
from sim.transports.socketcan_endpoint import SocketCanEndpoint

from .endpoints import QemuCanEndpoint


class CanTransportBridge:
    def __init__(self, context, stream, mapper, datagrams):
        self._context = context
        self._closed = False
        self.endpoint = QemuCanEndpoint(BorrowedCanPort(context, "qemu"), stream)
        self.endpoint.bind_mapper(mapper)
        self.socket_endpoint = (None if datagrams is None else SocketCanEndpoint(
            BorrowedCanPort(context, "socketcan"), datagrams))

    def input(self, port, frame):
        if self._closed:
            raise RuntimeError("CAN transport bridge is closed")
        if port == "qemu_rx":
            self.endpoint.ingest(frame, self._context.t_sim_ns)
        elif port == "socketcan_rx" and self.socket_endpoint is not None:
            self.socket_endpoint.ingest_frame(frame)
        else:
            raise ValueError(f"unknown CAN bridge input: {port}")

    def close(self):
        """Retire this graph endpoint without touching borrowed byte queues."""
        self._closed = True


class CanTransportBridgeFactory:
    """Explicitly inject transports; descriptions never read or write them."""

    def __init__(self, stream, mapper, datagrams=None):
        if not callable(getattr(stream, "send", None)):
            raise TypeError("QEMU CAN stream requires callable send")
        if any(not callable(getattr(mapper, name, None))
               for name in ("map", "unmap")):
            raise TypeError("QEMU CAN mapper requires callable map and unmap")
        check_can_time(mapper.qemu_origin_ns)
        if datagrams is not None and not callable(getattr(datagrams, "send", None)):
            raise TypeError("SocketCAN TX queue requires callable send")
        self._stream, self._mapper, self._datagrams = stream, mapper, datagrams

    def describe(self, spec):
        if spec.options:
            raise ValueError("CAN transport bridge has no scene options")
        names = ("qemu",) if self._datagrams is None else ("qemu", "socketcan")
        ports = tuple(PortSpec(name, "can", min_connections=1) for name in names)
        ports += tuple(PortSpec(f"{name}_rx", "can-frame", "in",
                                capabilities={"external_input"}) for name in names)
        return NodeDescription(ports, frozenset({"input"}))

    def validate_input(self, spec, port, payload, *, connected_ports):
        description = self.describe(spec)
        descriptor = description.port(port)
        if "external_input" not in descriptor.capabilities:
            raise ValueError(f"CAN bridge port does not accept external input: {port}")
        if port in connected_ports:
            raise ValueError("CAN bridge external input must not be internally connected")
        if not isinstance(payload, CanFrame):
            raise TypeError("CAN bridge input requires a CanFrame")
        return payload

    def validate_topology(self, topology, descriptions, registry):
        for node in topology.nodes:
            if registry[node.kind] is not self:
                continue
            for link in topology.links:
                for endpoint in (link.source, link.target):
                    if endpoint.node == node.name and endpoint.port.endswith("_rx"):
                        raise ValueError("CAN bridge external input must not be internally connected")

    def create(self, spec, context):
        return CanTransportBridge(context, self._stream, self._mapper, self._datagrams)
