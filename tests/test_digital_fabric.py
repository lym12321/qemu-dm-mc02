"""Digital protocol isolation: sampled logical levels, not electrical edges."""

from types import SimpleNamespace

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, Topology
from sim.fabric.digital import DigitalNet, DigitalNetFactory, DigitalSample


class Context:
    def __init__(self):
        self.events = []

    def emit(self, port, kind, payload, **metadata):
        self.events.append((port, kind, payload, metadata))


class Source:
    def __init__(self, value):
        self.value = value
        self.reads = 0

    def read_digital(self):
        self.reads += 1
        return self.value

    def close(self):
        pytest.fail('the net must not close its borrowed source')

    def reset(self, _session):
        pytest.fail('the net must not reset its borrowed source')


class Sink:
    def __init__(self, name, order):
        self.name, self.order = name, order
        self.error = None

    def receive_digital(self, level):
        if self.error:
            raise self.error
        self.order.append((self.name, level))

    def close(self):
        pytest.fail('the net must not close its borrowed sink')

    def reset(self, _session):
        pytest.fail('the net must not reset its borrowed sink')


def attach(net, endpoint, name, role):
    return net.attach_port(endpoint, descriptor=PortSpec('pin', 'digital', role, 'signal'),
                           endpoint_name=f'{name}.pin', owner_port='signal', context=object())


def wired(*, value=None, max_pending=256, dispatch_budget=256, sinks=2):
    context, order = Context(), []
    net = DigitalNet(context, max_pending=max_pending, dispatch_budget=dispatch_budget)
    source = Source(value if value is not None else DigitalSample(None, 100, 0))
    assert attach(net, source, 'source', 'out') is source
    receivers = [Sink(f'sink{index}', order) for index in range(sinks)]
    for receiver in receivers:
        attach(net, receiver, receiver.name, 'in')
    net.reset(Session(1))
    return net, source, receivers, context, order


def topology(*roles, options=None):
    nodes = [NodeSpec('wire', 'net.digital', options or {})]
    links = []
    for index, role in enumerate(roles):
        name = f'peer{index}'
        nodes.append(NodeSpec(name, 'fixture.endpoint', {'role': role}))
        pair = (f'{name}.pin', 'wire.signal')
        links.append(LinkSpec(*(reversed(pair) if index % 2 else pair)))
    endpoint_factory = SimpleNamespace(
        describe=lambda spec: NodeDescription((PortSpec('pin', 'digital', spec.options['role'], 'signal'),)),
        create=lambda *_args: pytest.fail('topology validation created a resource'))
    return Topology(tuple(nodes), tuple(links)), {
        'net.digital': DigitalNetFactory(), 'fixture.endpoint': endpoint_factory}


def test_pure_factory_admits_one_driver_and_multiple_sinks_independent_of_link_order():
    graph, registry = topology('in', 'out', 'in')
    descriptions = graph.validate(registry)
    description = descriptions['wire']
    assert description.capabilities == frozenset({'connection', 'dispatch', 'sample'})
    assert description.port('signal').max_connections is None


@pytest.mark.parametrize('roles', [('out', 'out'), ('in', 'in'), ('out', 'inout')])
def test_pure_factory_rejects_multiple_missing_or_ambiguous_drivers(roles):
    graph, registry = topology(*roles)
    with pytest.raises(ValueError, match='driver|input or output'):
        graph.validate(registry)


@pytest.mark.parametrize('options', [
    {'unknown': 1}, {'max_pending': 0}, {'dispatch_budget': True}, {'max_pending': 65537},
])
def test_factory_rejects_unknown_and_unbounded_queue_options(options):
    with pytest.raises(ValueError, match='digital'):
        DigitalNetFactory.describe(NodeSpec('wire', 'net.digital', options))


def test_unavailable_first_sample_changes_only_drive_at_dispatch_with_preserved_metadata():
    net, source, _, context, order = wired()
    assert source.reads == 0 and order == context.events == []
    net.sample(0)
    assert net.observe()['active'] is False and net.next_due_ns is None
    assert net.dispatch(0, 1) == 0 and order == []
    assert context.events[-1][1:3] == ('net.digital.sample', {
        'source': 'source.pin', 'level': None, 'active': False, 'changed': False})

    source.value = DigitalSample(0, 110, 10)
    net.sample(10)
    assert order == [] and net.pending == 1 and net.next_due_ns == 10
    assert net.dispatch(10, 0) == 0 and net.pending == 1
    assert net.dispatch(10, 1) == 1  # One broadcast, despite two receivers.
    assert order == [('sink0', 0), ('sink1', 0)]
    assert source.reads == 2 and net.next_due_ns is None
    first_delivery = context.events[-2]
    assert first_delivery[1:3] == ('net.digital.delivery', {
        'source': 'source.pin', 'destination': 'sink0.pin', 'level': 0,
        'sequence': 0, 'due_ns': 10})
    assert first_delivery[3] == {'source_time_ns': 110, 'mapped_time_ns': 10,
                                 'delivery_time_ns': 10, 'quality': 'sampled'}

    source.value = DigitalSample(0, 120, 20)
    net.sample(20)
    assert net.dispatch(20, 10) == 0
    source.value = DigitalSample(1, 130, 30)
    net.sample(30)
    assert net.dispatch(30, 10) == 1
    assert order[-2:] == [('sink0', 1), ('sink1', 1)]
    with pytest.raises(ValueError, match='once per completed cut'):
        net.sample(30)
    assert source.reads == 4
    with pytest.raises(TypeError):
        net.observe()['level'] = 0
    net.close()
    net.close()


def test_queue_is_ordered_bounded_and_uses_actual_broadcast_budget():
    net, source, _, _, order = wired(value=DigitalSample(0, 0, 0), max_pending=2,
                                     dispatch_budget=1)
    net.sample(0)
    source.value = DigitalSample(1, 1, 1)
    net.sample(1)
    assert net.pending == 2 and net.next_due_ns == 0
    assert net.dispatch(2, 99) == 1
    assert order == [('sink0', 0), ('sink1', 0)] and net.next_due_ns == 1
    assert net.dispatch(2, 99) == 1
    assert order[-2:] == [('sink0', 1), ('sink1', 1)] and net.pending == 0

    net.reset(Session(2, 10))
    source.value = DigitalSample(0, 10, 10)
    net.sample(10)
    source.value = DigitalSample(1, 11, 11)
    net.sample(11)
    source.value = DigitalSample(0, 12, 12)
    with pytest.raises(BufferError, match='capacity'):
        net.sample(12)
    assert net.observe()['failed'] is True and net.pending == 2
    assert net.observe()['level'] == 1
    with pytest.raises(RuntimeError, match='healthy'):
        net.dispatch(12, 1)
    net.close()


def test_losing_active_drive_fails_and_reset_never_reads_or_resets_borrowed_endpoints():
    net, source, _, _, order = wired(value=DigitalSample(1, 100, 0))
    net.sample(0)
    net.dispatch(0, 1)
    source.value = DigitalSample(None, 110, 10)
    with pytest.raises(RuntimeError, match='lost output drive'):
        net.sample(10)
    assert net.observe()['level'] == 1 and order == [('sink0', 1), ('sink1', 1)]
    with pytest.raises(RuntimeError, match='healthy'):
        net.sample(20)
    assert source.reads == 2
    net.reset(Session(2, 20))
    assert source.reads == 2 and net.next_due_ns is None
    source.value = DigitalSample(None, 120, 20)
    net.sample(20)
    assert net.observe()['active'] is False
    net.close()


def test_sink_failure_preserves_partial_effects_and_freezes_without_replaying_broadcast():
    net, source, receivers, context, order = wired(value=DigitalSample(1, 100, 0), sinks=3)
    error = OSError('sink refused level')
    receivers[1].error = error
    net.sample(0)
    with pytest.raises(OSError) as caught:
        net.dispatch(0, 1)
    assert caught.value is error
    assert any('source.pin to sink1.pin' in note for note in error.__notes__)
    assert order == [('sink0', 1)] and net.pending == 0
    assert [event[2]['destination'] for event in context.events
            if event[1] == 'net.digital.delivery'] == ['sink0.pin']
    with pytest.raises(RuntimeError, match='healthy'):
        net.dispatch(0, 1)
    assert source.reads == 1 and order == [('sink0', 1)]
    net.close()


@pytest.mark.parametrize('value', [True, False, -1, 2, 1.0, '1'])
def test_digital_sample_only_accepts_exact_integer_levels(value):
    with pytest.raises(ValueError, match='integer 0, 1 or None'):
        DigitalSample(value, 100, 0)


@pytest.mark.parametrize('value, match', [
    (None, 'DigitalSample'),
    (DigitalSample(1, 101, 11), 'beyond'),
    (DigitalSample(1, 99, 10), 'must not decrease'),
])
def test_invalid_source_results_freeze_without_delivery(value, match):
    net, source, _, _, order = wired(value=DigitalSample(0, 100, 0))
    net.sample(0)
    net.dispatch(0, 1)
    source.value = value
    with pytest.raises((TypeError, ValueError), match=match) as caught:
        net.sample(10)
    assert any('source.pin at 10 ns' in note for note in caught.value.__notes__)
    assert order == [('sink0', 0), ('sink1', 0)] and net.observe()['level'] == 0
    assert net.observe()['failed'] is True
    net.close()


def test_endpoint_admission_is_atomic_and_wiring_locks_before_first_source_read():
    context, order = Context(), []
    net = DigitalNet(context)
    with pytest.raises(ValueError, match='input or output'):
        attach(net, object(), 'ambiguous', 'inout')
    with pytest.raises(TypeError, match='read_digital'):
        attach(net, object(), 'missing', 'out')
    source = Source(DigitalSample(0, 0, 0))
    attach(net, source, 'source', 'out')
    with pytest.raises(ValueError, match='exactly one driver'):
        attach(net, Source(DigitalSample(1, 0, 0)), 'other', 'out')
    sink = Sink('sink', order)
    attach(net, sink, 'sink', 'in')
    with pytest.raises(ValueError, match='already attached'):
        attach(net, sink, 'duplicate', 'in')
    net.reset(Session(1))
    with pytest.raises(RuntimeError, match='before reset'):
        attach(net, Sink('late', order), 'late', 'in')
    assert source.reads == 0 and order == context.events == []
    net.close()


def test_fanout_is_bounded_before_callbacks_or_source_reads():
    # The source plus 255 sinks exhaust the topology's 256-link upper bound.
    order = []
    source = Source(DigitalSample(None, 100, 0))
    fresh = DigitalNet(Context())
    attach(fresh, source, 'source', 'out')
    for index in range(255):
        attach(fresh, Sink(f'sink{index}', order), f'sink{index}', 'in')
    with pytest.raises(BufferError, match='endpoint capacity'):
        attach(fresh, Sink('extra', order), 'extra', 'in')
    assert source.reads == 0 and order == []
    fresh.close()
