"""Bounded point-to-point UART bytes, without framing or an electrical clock.

Chunks are host read boundaries, not messages. Observation provenance survives
delivery, but neither delivery nor socket acceptance proves a firmware read.
Endpoints and their transports are borrowed; failures freeze partial effects.
"""

from dataclasses import dataclass
import heapq

from sim.api import Endpoint, NodeDescription, PortSpec, Session, TimeQuality
from sim.api.events import check_timestamp


def _options(options):
    defaults = {'max_pending': 256, 'max_pending_bytes': 65536,
                'dispatch_budget': 256, 'delay_ns': 0}
    unknown = set(options) - defaults.keys()
    if unknown:
        raise ValueError(f'unknown UART net options: {sorted(unknown)}')
    result = {**defaults, **options}
    check_timestamp(result['delay_ns'])
    for name in ('max_pending', 'dispatch_budget', 'max_pending_bytes'):
        maximum = 4096 * 65536 if name == 'max_pending_bytes' else 65536
        if type(result[name]) is not int or not 0 < result[name] <= maximum:
            raise ValueError(f'UART {name} must be an integer in 1..{maximum}')
    return result


class UartConnection:
    """Borrow a byte endpoint and the net's sole ordered queue."""

    def __init__(self, owner, name, context):
        self._owner, self._name, self._context = owner, name, context

    def send(self, data, *, source_time_ns=None, mapped_time_ns=None, quality='sampled'):
        self._context.check_io()
        self._owner._send(self._name, data, self._context,
                          source_time_ns=source_time_ns,
                          mapped_time_ns=mapped_time_ns, quality=quality)


@dataclass(frozen=True)
class _Chunk:
    source: str
    data: bytes
    source_time_ns: int | None
    mapped_time_ns: int
    quality: TimeQuality


class UartNet:
    """Own exactly two endpoints' byte queue; never close or reset the peers."""

    def __init__(self, context, **options):
        self._context = context
        self._options = _options(options)
        self._endpoints = {}
        self._queue = []
        self._pending_bytes = self._sequence = self._time_ns = 0
        self._last_mapped = {}
        self._initialized = self._closed = self._dispatching = False
        self._failed = None

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        if self._initialized or self._closed:
            raise RuntimeError('UART endpoints must attach before reset')
        if (not isinstance(descriptor, PortSpec) or descriptor.protocol != 'uart'
                or descriptor.category != 'protocol' or descriptor.direction != 'inout'
                or owner_port != 'uart'):
            raise ValueError('UART net requires inout UART protocol endpoints')
        Endpoint.parse(endpoint_name)
        if endpoint_name in self._endpoints or any(
                endpoint is peer for peer, _ in self._endpoints.values()):
            raise ValueError('UART endpoint is already attached')
        if len(self._endpoints) >= 2:
            raise ValueError('UART net requires exactly two endpoints')
        receive = getattr(endpoint, 'receive_uart', None)
        if not callable(receive):
            raise TypeError('UART endpoint must expose receive_uart(bytes)')
        self._endpoints[endpoint_name] = (endpoint, receive)
        return UartConnection(self, endpoint_name, context)

    @property
    def dispatch_budget(self):
        return self._options['dispatch_budget']

    @property
    def pending(self):
        return len(self._queue)

    @property
    def pending_bytes(self):
        return self._pending_bytes

    @property
    def next_due_ns(self):
        return self._queue[0][0] if self._queue else None

    def _check(self):
        if self._closed or not self._initialized or self._failed is not None:
            raise RuntimeError('UART net requires a successful reset and healthy open state')

    def reset(self, session):
        if self._closed or self._dispatching:
            raise RuntimeError('UART net cannot reset while closed or executing')
        if not isinstance(session, Session):
            raise TypeError('UART reset requires a Session')
        if len(self._endpoints) != 2:
            raise ValueError('UART net requires exactly two endpoints')
        self._queue.clear()
        self._last_mapped.clear()
        self._pending_bytes = self._sequence = 0
        self._time_ns = session.t_sim_ns
        self._failed = None
        self._initialized = True

    def _emit(self, kind, chunk, sequence, due, cut, destination=None):
        payload = {'source': chunk.source, 'data': chunk.data,
                   'sequence': sequence, 'due_ns': due}
        if destination is not None:
            payload['destination'] = destination
        self._context.emit('uart', kind, payload,
                           source_time_ns=chunk.source_time_ns,
                           mapped_time_ns=chunk.mapped_time_ns,
                           delivery_time_ns=cut, quality=chunk.quality)

    def _send(self, name, data, context, *, source_time_ns, mapped_time_ns, quality):
        self._check()
        if type(data) is not bytes:
            raise TypeError('UART data must be bytes')
        if len(data) > 4096:
            raise ValueError('one UART chunk is limited to 4096 bytes')
        if not data:
            return
        mapped = context.t_sim_ns if mapped_time_ns is None else mapped_time_ns
        check_timestamp(mapped)
        if source_time_ns is not None:
            check_timestamp(source_time_ns)
        quality = TimeQuality(quality)
        if name not in self._endpoints:
            raise ValueError('UART source is not attached')
        if mapped < self._last_mapped.get(name, 0):
            raise ValueError('UART source mapped time must not decrease')
        due = max(mapped, context.not_before_ns) + self._options['delay_ns']
        check_timestamp(due)
        try:
            if (self.pending >= self._options['max_pending']
                    or self.pending_bytes + len(data) > self._options['max_pending_bytes']):
                raise BufferError('UART pending chunk/byte capacity exhausted')
            chunk = _Chunk(name, data, source_time_ns, mapped, quality)
            sequence = self._sequence
            heapq.heappush(self._queue, (due, sequence, chunk))
            self._pending_bytes += len(data)
            self._sequence += 1
            self._last_mapped[name] = mapped
            self._emit('net.uart.sent', chunk, sequence, due, context.t_sim_ns)
        except BaseException as error:
            self._failed = error
            raise

    def dispatch(self, cut, budget):
        self._check()
        check_timestamp(cut)
        if cut < self._time_ns:
            raise ValueError('UART cut must not decrease')
        if type(budget) is not int or budget < 0:
            raise ValueError('UART dispatch budget must be a nonnegative integer')
        if self._dispatching:
            raise RuntimeError('UART dispatch cannot be reentered')
        self._time_ns = cut
        count = 0
        self._dispatching = True
        try:
            while self._queue and self._queue[0][0] <= cut and count < min(budget, self.dispatch_budget):
                due, sequence, chunk = heapq.heappop(self._queue)
                self._pending_bytes -= len(chunk.data)
                count += 1
                destination = next(name for name in self._endpoints if name != chunk.source)
                self._endpoints[destination][1](chunk.data)
                self._emit('net.uart.delivery', chunk, sequence, due, cut, destination)
            return count
        except BaseException as error:
            self._failed = error
            error.add_note(f'UART delivery failed at {cut} ns; partial effects are not replayed')
            raise
        finally:
            self._dispatching = False

    def close(self):
        if self._dispatching:
            raise RuntimeError('UART net cannot close during dispatch')
        self._closed = True
        self._queue.clear()
        self._pending_bytes = 0
        self._endpoints.clear()


class UartNetFactory:
    @staticmethod
    def describe(spec):
        _options(spec.options)
        return NodeDescription((PortSpec('uart', 'uart', min_connections=2, max_connections=2),),
                               frozenset({'connection', 'dispatch'}))

    @classmethod
    def validate_topology(cls, topology, descriptions, registry):
        for node in topology.nodes:
            if not isinstance(registry[node.kind], cls):
                continue
            for link in topology.links:
                for owner, peer in ((link.source, link.target), (link.target, link.source)):
                    if owner.node == node.name:
                        descriptor = descriptions[peer.node].port(peer.port)
                        if (descriptor.protocol != 'uart' or descriptor.category != 'protocol'
                                or descriptor.direction != 'inout'):
                            raise ValueError(f'UART net requires inout UART endpoints: {peer}')

    @staticmethod
    def create(spec, context):
        return UartNet(context, **spec.options)
