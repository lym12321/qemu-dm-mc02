"""Timestamped digital events from explicit producers, never GPIO snapshots.

The producer owns its clock mapping and the evidence for each edge. This
point-to-multipoint owner preserves every edge and enforces the current cut
floor. It neither samples levels nor reconstructs events between samples.
"""

from collections import deque
from dataclasses import dataclass

from sim.api import Endpoint, NodeDescription, PortSpec, Session
from sim.api.events import check_timestamp


@dataclass(frozen=True)
class DigitalEdge:
    level: int
    source_time_ns: int
    mapped_time_ns: int

    def __post_init__(self):
        if type(self.level) is not int or self.level not in (0, 1):
            raise ValueError('digital edge level must be integer 0 or 1')
        check_timestamp(self.source_time_ns)
        check_timestamp(self.mapped_time_ns)


def _options(options):
    if set(options) - {'max_pending', 'dispatch_budget'}:
        raise ValueError('unknown digital-edge net options')
    result = {}
    for name in ('max_pending', 'dispatch_budget'):
        value = options.get(name, 256)
        if type(value) is not int or not 0 < value <= 65536:
            raise ValueError(f'digital-edge {name} must be an integer in 1..65536')
        result[name] = value
    return result


class _EdgeOutput:
    def __init__(self, net, context):
        self._net, self._context = net, context

    def send(self, edge):
        self._context.check_io()
        self._net._send(edge, self._context.not_before_ns)


class DigitalEdgeNet:
    def __init__(self, context, *, max_pending=256, dispatch_budget=256):
        options = _options({'max_pending': max_pending, 'dispatch_budget': dispatch_budget})
        self._context = context
        self.max_pending = options['max_pending']
        self.dispatch_budget = options['dispatch_budget']
        self._source = None
        self._endpoints = {}
        self._sinks = []
        self._queue = deque()
        self._last_edge = None
        self._sequence = self._time_ns = 0
        self._initialized = self._closed = self._busy = False
        self._failed = None

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        if self._closed or self._initialized:
            raise RuntimeError('digital-edge wiring must precede reset')
        if (owner_port != 'signal' or descriptor.protocol != 'digital-edge'
                or descriptor.category != 'signal' or descriptor.direction not in ('in', 'out')):
            raise ValueError('digital-edge endpoints must be input or output signals')
        Endpoint.parse(endpoint_name)
        if id(endpoint) in self._endpoints or endpoint_name in self._endpoints.values():
            raise ValueError('digital-edge endpoint is already attached')
        if len(self._endpoints) >= 256:
            raise BufferError('digital-edge endpoint capacity exhausted')
        if descriptor.direction == 'out':
            if 'event-output' not in descriptor.capabilities:
                raise ValueError('digital-edge source requires event-output capability')
            if self._source is not None:
                raise ValueError('digital-edge net permits one source')
            self._source = endpoint_name
            result = _EdgeOutput(self, context)
        else:
            receive = getattr(endpoint, 'receive_edge', None)
            if not callable(receive):
                raise TypeError('digital-edge sink requires receive_edge')
            self._sinks.append((endpoint_name, receive))
            result = endpoint
        self._endpoints[id(endpoint)] = endpoint_name
        return result

    @property
    def next_due_ns(self):
        self._raise_failure()
        return self._queue[0][0] if self._queue else None

    @property
    def pending(self):
        return len(self._queue)

    def reset(self, session):
        if self._closed or self._busy or not isinstance(session, Session):
            raise RuntimeError('digital-edge reset requires an open idle owner and Session')
        if self._source is None or not self._sinks:
            raise ValueError('digital-edge net requires one source and at least one sink')
        self._queue.clear()
        self._last_edge = None
        self._sequence = 0
        self._time_ns = session.t_sim_ns
        self._failed = None
        self._initialized = True

    def _check(self):
        if self._closed or not self._initialized or self._failed is not None:
            raise RuntimeError('digital-edge net requires a healthy open session')

    def _raise_failure(self):
        if self._failed is not None:
            raise self._failed

    def _send(self, edge, floor):
        self._check()
        try:
            if not isinstance(edge, DigitalEdge):
                raise TypeError('digital-edge send requires DigitalEdge')
            check_timestamp(floor)
            if self._last_edge is not None and (
                    edge.source_time_ns < self._last_edge.source_time_ns
                    or edge.mapped_time_ns < self._last_edge.mapped_time_ns):
                raise ValueError('digital-edge source and mapped times must not decrease')
            if len(self._queue) >= self.max_pending:
                raise BufferError('digital-edge pending capacity exhausted')
            due = max(edge.mapped_time_ns, floor, self._time_ns)
            self._queue.append((due, self._sequence, edge))
            self._sequence += 1
            self._last_edge = edge
        except BaseException as error:
            if self._failed is None:
                self._failed = error
            raise self._failed

    def dispatch(self, cut, budget):
        self._check()
        if self._busy:
            raise RuntimeError('digital-edge dispatch cannot be reentered')
        check_timestamp(cut)
        if cut < self._time_ns or type(budget) is not int or budget < 0:
            raise ValueError('digital-edge cut must not decrease and budget must be nonnegative')
        self._time_ns = cut
        self._busy = True
        count = 0
        try:
            while self._queue and self._queue[0][0] <= cut and count < min(budget, self.dispatch_budget):
                due, sequence, edge = self._queue.popleft()
                count += 1
                for name, receive in self._sinks:
                    receive(edge)
                    self._raise_failure()
                    self._context.emit('signal', 'net.digital-edge.delivery', {
                        'source': self._source, 'destination': name, 'level': edge.level,
                        'sequence': sequence, 'due_ns': due,
                    }, source_time_ns=edge.source_time_ns, mapped_time_ns=edge.mapped_time_ns,
                        delivery_time_ns=cut, quality='exact')
                    self._raise_failure()
            return count
        except BaseException as error:
            if self._failed is None:
                self._failed = error
            raise self._failed
        finally:
            self._busy = False

    def close(self):
        if self._busy:
            raise RuntimeError('digital-edge owner cannot close during dispatch')
        self._closed = True
        self._queue.clear()
        self._endpoints.clear()
        self._sinks.clear()


class DigitalEdgeNetFactory:
    def describe(self, spec):
        _options(spec.options)
        return NodeDescription((PortSpec('signal', 'digital-edge', category='signal',
                                        max_connections=256, min_connections=2),),
                               {'connection', 'dispatch'})

    def validate_topology(self, topology, descriptions, registry):
        for node in topology.nodes:
            if not isinstance(registry[node.kind], DigitalEdgeNetFactory):
                continue
            roles = []
            for link in topology.links:
                for owner, peer in ((link.source, link.target), (link.target, link.source)):
                    if owner.node == node.name:
                        port = descriptions[peer.node].port(peer.port)
                        if port.direction == 'out' and 'event-output' not in port.capabilities:
                            raise ValueError('digital-edge source requires event-output capability')
                        roles.append(port.direction)
            if roles.count('out') != 1 or roles.count('in') < 1 or 'inout' in roles:
                raise ValueError('digital-edge net requires one output and at least one input')

    def create(self, spec, context):
        return DigitalEdgeNet(context, **_options(spec.options))
