"""Private bounded fanout for completed-state signal protocols.

Protocol subclasses supply sample admission, publication and sink values. This
owner alone holds wiring, queued broadcasts and the failure lifecycle; it never
resets or closes the endpoints it borrows.
"""

from collections import deque

from sim.api import Endpoint, NodeDescription, PortSpec, Session
from sim.api.events import check_timestamp


_MAX_ENDPOINTS = 256  # The topology's existing total link bound.


def _options(options, protocol):
    unknown = set(options) - {'max_pending', 'dispatch_budget'}
    if unknown:
        raise ValueError(f'unknown {protocol} net options: {sorted(unknown)}')
    result = {}
    for name in ('max_pending', 'dispatch_budget'):
        value = options.get(name, 256)
        if type(value) is not int or not 0 < value <= 65536:
            raise ValueError(f'{protocol} {name} must be an integer in 1..65536')
        result[name] = value
    return result


class _SampledNetFactory:
    @classmethod
    def describe(cls, spec):
        _options(spec.options, cls._protocol)
        return NodeDescription((PortSpec('signal', cls._protocol, category='signal',
                                        min_connections=2, max_connections=None),),
                               frozenset({'connection', 'dispatch', 'sample'}))

    @classmethod
    def validate_topology(cls, topology, descriptions, registry):
        for node in topology.nodes:
            if not isinstance(registry[node.kind], cls):
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
                        raise ValueError(f'{cls._protocol} net endpoint must be input or output: {peer}')
            if drivers != 1 or sinks < 1:
                raise ValueError(f'{cls._protocol} net {node.name} requires exactly one driver '
                                 'and at least one sink')

    @classmethod
    def create(cls, spec, context):
        return cls._net_type(context, **_options(spec.options, cls._protocol))


class _SampledFanout:
    def __init__(self, context, *, max_pending=256, dispatch_budget=256):
        options = _options({'max_pending': max_pending, 'dispatch_budget': dispatch_budget},
                           self._protocol)
        if not callable(getattr(context, 'emit', None)):
            raise TypeError(f'{self._protocol} net context must expose emit')
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
        self._sample_cut = self._sample = None
        self._sequence = 0
        self._reset_sample_state()

    def attach_port(self, endpoint, *, descriptor, endpoint_name, owner_port, context):
        """Borrow an endpoint; pull-only sources expose no push/send method."""
        if self._closed or self._initialized:
            raise RuntimeError(f'{self._protocol} wiring must attach before reset')
        if (not isinstance(descriptor, PortSpec) or descriptor.protocol != self._protocol
                or descriptor.category != 'signal' or descriptor.direction not in ('in', 'out')
                or owner_port != 'signal'):
            raise ValueError(f'{self._protocol} endpoint must be an input or output '
                             f'{self._protocol} signal')
        Endpoint.parse(endpoint_name)
        if id(endpoint) in self._endpoints or any(
                name == endpoint_name for _, name in self._endpoints.values()):
            raise ValueError(f'{self._protocol} endpoint is already attached')
        if len(self._endpoints) >= _MAX_ENDPOINTS:
            raise BufferError(f'{self._protocol} endpoint capacity exhausted')
        method_name = f'read_{self._protocol}' if descriptor.direction == 'out' else f'receive_{self._protocol}'
        method = getattr(endpoint, method_name, None)
        if not callable(method):
            raise TypeError(f'{self._protocol} endpoint must expose {method_name}')
        if descriptor.direction == 'out':
            if self._driver is not None:
                raise ValueError(f'{self._protocol} net permits exactly one driver')
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
            raise RuntimeError(f'{self._protocol} net cannot reset while closed or executing')
        if not isinstance(session, Session):
            raise TypeError(f'{self._protocol} reset requires a Session')
        check_timestamp(session.t_sim_ns)
        if self._driver is None or not self._sinks:
            raise ValueError(f'{self._protocol} net requires one driver and at least one sink')
        self._queue.clear()
        self._time_ns = session.t_sim_ns
        self._sample_cut = self._sample = None
        self._reset_sample_state()
        self._sequence = 0
        self._failed = None
        self._initialized = True

    def _check(self, cut):
        if self._closed or not self._initialized or self._failed is not None:
            raise RuntimeError(f'{self._protocol} net requires a successful reset and healthy open state')
        if self._busy:
            raise RuntimeError(f'{self._protocol} net execution cannot be reentered')
        check_timestamp(cut)
        if cut < self._time_ns:
            raise ValueError(f'{self._protocol} cut must not decrease')

    def _emit(self, kind, payload, sample, cut):
        self._context.emit('signal', kind, payload,
                           source_time_ns=sample.source_time_ns,
                           mapped_time_ns=sample.mapped_time_ns,
                           delivery_time_ns=cut, quality='sampled')

    def sample(self, cut):
        self._check(cut)
        if self._sample_cut is not None and cut <= self._sample_cut:
            raise ValueError(f'{self._protocol} sources may be sampled only once per completed cut')
        self._busy = True
        try:
            sample = self._driver[1]()
            enqueue = self._validate_sample(sample, cut)
            if enqueue and len(self._queue) >= self.max_pending:
                raise BufferError(f'{self._protocol} pending sample capacity exhausted')
            self._time_ns = self._sample_cut = cut
            self._sample = sample
            self._commit_sample(sample)
            if enqueue:
                self._queue.append((cut, self._sequence, sample))
                self._sequence += 1
            payload = {'source': self._driver[0], **self._sample_payload(sample, enqueue)}
            self._emit(f'net.{self._protocol}.sample', payload, sample, cut)
        except BaseException as error:
            self._failed = error
            error.add_note(f'{self._protocol} sampling failed: {self._driver[0]} at {cut} ns')
            raise
        finally:
            self._busy = False

    def dispatch(self, cut, budget):
        self._check(cut)
        if type(budget) is not int or budget < 0:
            raise ValueError(f'{self._protocol} dispatch budget must be a nonnegative integer')
        self._time_ns = cut
        count = 0
        self._busy = True
        try:
            while self._queue and self._queue[0][0] <= cut and count < min(budget, self.dispatch_budget):
                due, sequence, sample = self._queue.popleft()
                count += 1
                for name, receive in self._sinks:
                    try:
                        receive(self._delivery_value(sample))
                        payload = {'source': self._driver[0], 'destination': name,
                                   **self._delivery_payload(sample), 'sequence': sequence, 'due_ns': due}
                        self._emit(f'net.{self._protocol}.delivery', payload, sample, cut)
                    except BaseException as error:
                        error.add_note(f'{self._protocol} delivery failed: {self._driver[0]} '
                                       f'to {name} at {cut} ns')
                        raise
            return count
        except BaseException as error:
            self._failed = error
            raise
        finally:
            self._busy = False

    def close(self):
        if self._busy:
            raise RuntimeError(f'{self._protocol} net cannot close during execution')
        self._closed = True
        self._queue.clear()
        self._driver = None
        self._sinks.clear()
        self._endpoints.clear()
