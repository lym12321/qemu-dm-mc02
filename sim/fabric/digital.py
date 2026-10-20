"""Sampled digital wiring over completed states, without an electrical solver.

One driver feeds bounded sinks in attachment order. An unavailable initial
source leaves inputs untouched; losing output drive after activation fails.
Sampling and delivery are separate phases, so sink writes never cause another
source read at the same cut. Native source time is observation metadata, not an
edge timestamp or a firmware-consumption receipt.
"""

from collections import deque
from dataclasses import dataclass

from sim.api import Endpoint, NodeDescription, PortSpec, Session
from sim.api.events import check_timestamp
from sim.api.topology import immutable


_MAX_ENDPOINTS = 256  # The topology's existing total link bound.


@dataclass(frozen=True)
class DigitalSample:
    level: int | None
    source_time_ns: int
    mapped_time_ns: int

    def __post_init__(self):
        if self.level is not None and (type(self.level) is not int or self.level not in (0, 1)):
            raise ValueError('digital level must be integer 0, 1 or None')
        check_timestamp(self.source_time_ns)
        check_timestamp(self.mapped_time_ns)


def _options(options):
    unknown = set(options) - {'max_pending', 'dispatch_budget'}
    if unknown:
        raise ValueError(f'unknown digital net options: {sorted(unknown)}')
    result = {}
    for name in ('max_pending', 'dispatch_budget'):
        value = options.get(name, 256)
        if type(value) is not int or not 0 < value <= 65536:
            raise ValueError(f'digital {name} must be an integer in 1..65536')
        result[name] = value
    return result


class DigitalNetFactory:
    @staticmethod
    def describe(spec):
        _options(spec.options)
        return NodeDescription((PortSpec('signal', 'digital', category='signal',
                                        min_connections=2, max_connections=None),),
                               frozenset({'connection', 'dispatch', 'sample'}))

    @staticmethod
    def validate_topology(topology, descriptions, registry):
        for node in topology.nodes:
            if not isinstance(registry[node.kind], DigitalNetFactory):
                continue
            drivers = sinks = 0
            for link in topology.links:
                for owner, peer in ((link.source, link.target), (link.target, link.source)):
                    if owner.node != node.name:
                        continue
                    role = descriptions[peer.node].port(peer.port).direction
                    if role == 'out':
                        drivers += 1
                    elif role == 'in':
                        sinks += 1
                    else:
                        raise ValueError(f'digital net endpoint must be input or output: {peer}')
            if drivers != 1 or sinks < 1:
                raise ValueError(f'digital net {node.name} requires exactly one driver and at least one sink')

    @staticmethod
    def create(spec, context):
        return DigitalNet(context, **_options(spec.options))


class DigitalNet:
    """Own sampled broadcasts while borrowing every endpoint and its context."""

    def __init__(self, context, *, max_pending=256, dispatch_budget=256):
        options = _options({'max_pending': max_pending, 'dispatch_budget': dispatch_budget})
        if not callable(getattr(context, 'emit', None)):
            raise TypeError('digital net context must expose emit')
        self._context = context
        self.max_pending = options['max_pending']
        self._dispatch_budget = options['dispatch_budget']
        self._driver = None
        self._sinks = []
        self._endpoints = {}
        self._queue = deque()
        self._initialized = self._closed = self._busy = False
        self._failed = None
        self._time_ns = 0
        self._sample_cut = self._sample = self._level = None
        self._active = False
        self._sequence = 0

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        """Borrow an endpoint; pull-only sources expose no push/send method."""
        if self._closed or self._initialized:
            raise RuntimeError('digital wiring must attach before reset')
        if (not isinstance(descriptor, PortSpec) or descriptor.protocol != 'digital'
                or descriptor.category != 'signal' or descriptor.direction not in ('in', 'out')
                or owner_port != 'signal'):
            raise ValueError('digital endpoint must be an input or output digital signal')
        Endpoint.parse(endpoint_name)
        if id(endpoint) in self._endpoints or any(
                name == endpoint_name for _, name in self._endpoints.values()):
            raise ValueError('digital endpoint is already attached')
        if len(self._endpoints) >= _MAX_ENDPOINTS:
            raise BufferError('digital endpoint capacity exhausted')
        method_name = 'read_digital' if descriptor.direction == 'out' else 'receive_digital'
        method = getattr(endpoint, method_name, None)
        if not callable(method):
            raise TypeError(f'digital endpoint must expose {method_name}')
        if descriptor.direction == 'out':
            if self._driver is not None:
                raise ValueError('digital net permits exactly one driver')
            self._driver = (endpoint_name, method)
        else:
            self._sinks.append((endpoint_name, method))
        self._endpoints[id(endpoint)] = (endpoint, endpoint_name)
        return endpoint

    @property
    def dispatch_budget(self):
        return self._dispatch_budget

    @property
    def pending(self):
        return len(self._queue)

    @property
    def next_due_ns(self):
        return self._queue[0][0] if self._queue else None

    def reset(self, session):
        if self._closed or self._busy:
            raise RuntimeError('digital net cannot reset while closed or executing')
        if not isinstance(session, Session):
            raise TypeError('digital reset requires a Session')
        check_timestamp(session.t_sim_ns)
        if self._driver is None or not self._sinks:
            raise ValueError('digital net requires one driver and at least one sink')
        self._queue.clear()
        self._time_ns = session.t_sim_ns
        self._sample_cut = self._sample = self._level = None
        self._active = False
        self._sequence = 0
        self._failed = None
        self._initialized = True

    def _check(self, cut):
        if self._closed or not self._initialized or self._failed is not None:
            raise RuntimeError('digital net requires a successful reset and healthy open state')
        if self._busy:
            raise RuntimeError('digital net execution cannot be reentered')
        check_timestamp(cut)
        if cut < self._time_ns:
            raise ValueError('digital cut must not decrease')

    def _emit(self, kind, payload, sample, cut):
        self._context.emit('signal', kind, payload,
                           source_time_ns=sample.source_time_ns,
                           mapped_time_ns=sample.mapped_time_ns,
                           delivery_time_ns=cut, quality='sampled')

    def sample(self, cut):
        self._check(cut)
        if self._sample_cut is not None and cut <= self._sample_cut:
            raise ValueError('digital sources may be sampled only once per completed cut')
        self._busy = True
        try:
            sample = self._driver[1]()
            if not isinstance(sample, DigitalSample):
                raise TypeError(f'digital source {self._driver[0]} must return a DigitalSample')
            if sample.mapped_time_ns > cut:
                raise ValueError(f'digital source {self._driver[0]} sample is beyond the completed cut')
            if self._sample is not None and (
                    sample.mapped_time_ns < self._sample.mapped_time_ns
                    or sample.source_time_ns < self._sample.source_time_ns):
                raise ValueError(f'digital source {self._driver[0]} sample time must not decrease')
            if self._active and sample.level is None:
                raise RuntimeError(f'digital source {self._driver[0]} lost output drive after activation; '
                                   'release is unsupported')
            changed = sample.level is not None and sample.level != self._level
            if changed and len(self._queue) >= self.max_pending:
                raise BufferError('digital pending sample capacity exhausted')
            self._time_ns = self._sample_cut = cut
            self._sample, self._level = sample, sample.level
            self._active = sample.level is not None
            if changed:
                self._queue.append((cut, self._sequence, sample))
                self._sequence += 1
            self._emit('net.digital.sample', {'source': self._driver[0],
                       'level': sample.level, 'active': self._active, 'changed': changed}, sample, cut)
        except BaseException as error:
            self._failed = error
            error.add_note(f'digital sampling failed: {self._driver[0]} at {cut} ns')
            raise
        finally:
            self._busy = False

    def dispatch(self, cut, budget):
        self._check(cut)
        if type(budget) is not int or budget < 0:
            raise ValueError('digital dispatch budget must be a nonnegative integer')
        self._time_ns = cut
        count = 0
        self._busy = True
        try:
            while self._queue and self._queue[0][0] <= cut and count < min(budget, self.dispatch_budget):
                due, sequence, sample = self._queue.popleft()
                count += 1
                for name, receive in self._sinks:
                    try:
                        receive(sample.level)
                        self._emit('net.digital.delivery', {'source': self._driver[0],
                                   'destination': name, 'level': sample.level, 'sequence': sequence,
                                   'due_ns': due}, sample, cut)
                    except BaseException as error:
                        error.add_note(f'digital delivery failed: {self._driver[0]} to {name} at {cut} ns')
                        raise
            return count
        except BaseException as error:
            self._failed = error
            raise
        finally:
            self._busy = False

    def observe(self):
        return immutable({'active': self._active, 'level': self._level,
                          'sample_time_ns': self._sample_cut, 'pending': self.pending,
                          'failed': self._failed is not None})

    def close(self):
        if self._busy:
            raise RuntimeError('digital net cannot close during execution')
        self._closed = True
        self._queue.clear()
        self._driver = None
        self._sinks.clear()
        self._endpoints.clear()
