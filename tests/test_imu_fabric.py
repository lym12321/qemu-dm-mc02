"""IMU signal isolation: completed SI values, bounded ordered broadcasts."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from sim.api import ImuSample, ImuSignalSample, LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, Topology
from sim.fabric.imu import ImuNet, ImuNetFactory


VALUE = ImuSample((1.0, -2.0, 3.0), (0.0, 0.0, 9.80665))


class Context:
    def __init__(self):
        self.events = []

    def emit(self, port, kind, payload, **metadata):
        self.events.append((port, kind, payload, metadata))


class Source:
    def __init__(self, value):
        self.value, self.reads = value, 0

    def read_imu(self):
        self.reads += 1
        return self.value() if callable(self.value) else self.value

    def close(self):
        pytest.fail('the net must not close its borrowed source')

    def reset(self, _session):
        pytest.fail('the net must not reset its borrowed source')


class Sink:
    def __init__(self, name, order):
        self.name, self.order, self.error = name, order, None

    def receive_imu(self, sample):
        if self.error:
            raise self.error
        self.order.append((self.name, sample))

    def close(self):
        pytest.fail('the net must not close its borrowed sink')

    def reset(self, _session):
        pytest.fail('the net must not reset its borrowed sink')


def attach(net, endpoint, name, role):
    return net.attach_port(endpoint, descriptor=PortSpec('imu', 'imu', role, 'signal'),
                           endpoint_name=f'{name}.imu', owner_port='signal', context=object())


def wired(*, max_pending=256, dispatch_budget=256, sinks=2):
    context, order = Context(), []
    net = ImuNet(context, max_pending=max_pending, dispatch_budget=dispatch_budget)
    source = Source(ImuSignalSample(VALUE, None, 0))
    assert attach(net, source, 'source', 'out') is source
    receivers = [Sink(f'sink{index}', order) for index in range(sinks)]
    for receiver in receivers:
        attach(net, receiver, receiver.name, 'in')
    net.reset(Session(1))
    return net, source, receivers, context, order


def topology(*roles):
    nodes = [NodeSpec('wire', 'net.imu')]
    links = []
    for index, role in enumerate(roles):
        name = f'peer{index}'
        nodes.append(NodeSpec(name, 'fixture.endpoint', {'role': role}))
        pair = (f'{name}.imu', 'wire.signal')
        links.append(LinkSpec(*(reversed(pair) if index % 2 else pair)))
    factory = SimpleNamespace(
        describe=lambda spec: NodeDescription((PortSpec('imu', 'imu', spec.options['role'], 'signal'),)),
        create=lambda *_args: pytest.fail('topology validation created a resource'))
    return Topology(tuple(nodes), tuple(links)), {'net.imu': ImuNetFactory(), 'fixture.endpoint': factory}


def test_public_imu_value_is_the_same_type_at_existing_physics_imports():
    from sim.physics import ImuSample as PublicPhysicsSample
    from sim.physics.base import ImuSample as PhysicsSample

    assert ImuSample is PhysicsSample is PublicPhysicsSample


def test_pure_factory_admits_ordered_sinks_and_one_driver_with_unbounded_port_cardinality():
    graph, registry = topology('in', 'out', 'in')
    description = graph.validate(registry)['wire']
    assert description.capabilities == frozenset({'connection', 'dispatch', 'sample'})
    port = description.port('signal')
    assert port.protocol == 'imu' and port.category == 'signal'
    assert port.min_connections == 2 and port.max_connections is None


@pytest.mark.parametrize('roles', [('out', 'out'), ('in', 'in'), ('out', 'inout')])
def test_pure_factory_rejects_multiple_missing_or_ambiguous_drivers(roles):
    graph, registry = topology(*roles)
    with pytest.raises(ValueError, match='driver|input or output'):
        graph.validate(registry)


@pytest.mark.parametrize('options', [
    {'unknown': 1}, {'max_pending': 0}, {'dispatch_budget': True}, {'max_pending': 65537},
])
def test_factory_rejects_unknown_and_unbounded_options(options):
    with pytest.raises(ValueError, match='imu'):
        ImuNetFactory.describe(NodeSpec('wire', 'net.imu', options))


def test_each_completed_cut_broadcasts_even_equal_si_values_with_original_metadata():
    net, source, _, context, order = wired()
    assert source.reads == 0 and order == context.events == []
    for cut, stamp in ((0, None), (7, 100), (10, 100)):
        sample = ImuSignalSample(VALUE, stamp, cut)
        source.value = sample
        net.sample(cut)
        assert net.pending == 1 and net.next_due_ns == cut
        assert net.dispatch(cut, 0) == 0
        assert net.dispatch(cut, 1) == 1  # A two-sink broadcast consumes one budget.
        assert order[-2:] == [('sink0', sample), ('sink1', sample)]
        assert order[-1][1] is sample
        assert context.events[-3][1:3] == ('net.imu.sample', {'source': 'source.imu', 'sample': sample})
        assert context.events[-2][1:3] == ('net.imu.delivery', {
            'source': 'source.imu', 'destination': 'sink0.imu', 'sample': sample,
            'sequence': len(order) // 2 - 1, 'due_ns': cut})
        assert context.events[-1][3] == {'source_time_ns': stamp, 'mapped_time_ns': cut,
                                        'delivery_time_ns': cut, 'quality': 'sampled'}
    assert source.reads == 3 and len(order) == 6
    with pytest.raises(ValueError, match='once per completed cut'):
        net.sample(10)
    assert source.reads == 3 and net.observe()['failed'] is False
    with pytest.raises(TypeError):
        net.observe()['sample_time_ns'] = 0
    with pytest.raises(FrozenInstanceError):
        order[-1][1].sample = VALUE
    net.close()
    net.close()


def test_queue_capacity_refuses_entire_sample_and_dispatch_counts_broadcasts():
    net, source, _, _, order = wired(max_pending=2, dispatch_budget=1)
    net.sample(0)
    source.value = ImuSignalSample(VALUE, None, 1)
    net.sample(1)
    assert net.pending == 2
    assert net.dispatch(1, 99) == 1 and len(order) == 2 and net.next_due_ns == 1
    source.value = ImuSignalSample(VALUE, None, 2)
    net.sample(2)
    source.value = ImuSignalSample(VALUE, None, 3)
    with pytest.raises(BufferError, match='capacity'):
        net.sample(3)
    assert net.pending == 2 and net.observe()['sample_time_ns'] == 2
    assert net.observe()['sample'].mapped_time_ns == 2 and net.observe()['failed'] is True
    with pytest.raises(RuntimeError, match='healthy'):
        net.dispatch(3, 1)
    assert len(order) == 2
    net.close()


def test_last_known_source_stamp_survives_unknown_stamps_and_reset_rebuilds_state():
    net, source, _, _, order = wired()
    for cut, stamp in ((0, 100), (1, None), (2, 101), (3, None)):
        source.value = ImuSignalSample(VALUE, stamp, cut)
        net.sample(cut)
        net.dispatch(cut, 1)
    source.value = ImuSignalSample(VALUE, 100, 4)
    with pytest.raises(ValueError, match='must not decrease'):
        net.sample(4)
    assert len(order) == 8 and net.observe()['sample_time_ns'] == 3
    reads = source.reads
    net.reset(Session(2, 10))
    assert source.reads == reads and net.observe()['sample'] is None
    source.value = ImuSignalSample(VALUE, 0, 10)
    net.sample(10)
    net.dispatch(10, 1)
    assert len(order) == 10
    net.close()


@pytest.mark.parametrize('produce, match', [
    (lambda: None, 'ImuSignalSample'),
    (lambda: VALUE, 'ImuSignalSample'),
    (lambda: ImuSignalSample(VALUE, None, 9), 'completed cut'),
    (lambda: ImuSignalSample(VALUE, None, 11), 'completed cut'),
    (lambda: ImuSignalSample(ImuSample((float('nan'), 0.0, 0.0), (0.0, 0.0, 0.0)), None, 10), 'finite'),
    (lambda: ImuSignalSample(ImuSample((0.0, 0.0, 0.0), [0.0, 0.0, 0.0]), None, 10), 'tuple'),
    (lambda: ImuSignalSample(VALUE, True, 10), 'unsigned 64-bit'),
])
def test_invalid_envelopes_or_si_freeze_without_delivery_or_new_completed_state(produce, match):
    net, source, _, _, order = wired()
    net.sample(0)
    net.dispatch(0, 1)
    source.value = produce
    with pytest.raises((TypeError, ValueError), match=match) as caught:
        net.sample(10)
    assert any('source.imu at 10 ns' in note for note in caught.value.__notes__)
    assert len(order) == 2 and net.observe()['sample_time_ns'] == 0
    assert net.observe()['failed'] is True
    with pytest.raises(RuntimeError, match='healthy'):
        net.sample(20)
    assert source.reads == 2
    net.close()


def test_partial_sink_failure_preserves_effects_and_never_replays():
    net, source, sinks, context, order = wired(sinks=3)
    error = OSError('refused SI observation')
    sinks[1].error = error
    net.sample(0)
    with pytest.raises(OSError) as caught:
        net.dispatch(0, 1)
    assert caught.value is error
    assert any('source.imu to sink1.imu at 0 ns' in note for note in error.__notes__)
    assert order == [('sink0', source.value)] and net.pending == 0
    assert [event[2]['destination'] for event in context.events
            if event[1] == 'net.imu.delivery'] == ['sink0.imu']
    with pytest.raises(RuntimeError, match='healthy'):
        net.dispatch(0, 1)
    assert source.reads == 1 and len(order) == 1
    net.close()


def test_endpoint_admission_is_atomic_and_bounded_before_reads_or_callbacks():
    net, order = ImuNet(Context()), []
    with pytest.raises(ValueError, match='input or output'):
        attach(net, object(), 'ambiguous', 'inout')
    with pytest.raises(TypeError, match='read_imu'):
        attach(net, object(), 'missing', 'out')
    source = Source(ImuSignalSample(VALUE, None, 0))
    attach(net, source, 'source', 'out')
    with pytest.raises(ValueError, match='exactly one driver'):
        attach(net, Source(source.value), 'duplicate_source', 'out')
    for index in range(255):
        attach(net, Sink(f'sink{index}', order), f'sink{index}', 'in')
    with pytest.raises(BufferError, match='endpoint capacity'):
        attach(net, Sink('extra', order), 'extra', 'in')
    net.reset(Session(1))
    with pytest.raises(RuntimeError, match='before reset'):
        attach(net, Sink('late', order), 'late', 'in')
    assert source.reads == 0 and order == []
    net.close()
