"""Opaque byte order, bounded ownership and direct generic-runtime consumers."""

from types import SimpleNamespace

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, Topology
from sim.devices.uart_echo import UartEchoFactory
from sim.fabric.uart import UartNet, UartNetFactory
from sim.runtime import Simulation, SimulationState


class Context:
    def __init__(self):
        self.t_sim_ns = self.not_before_ns = 0
        self.events = []

    def check_io(self):
        pass

    def emit(self, port, kind, payload, **stamp):
        self.events.append((port, kind, payload, stamp))


class Peer:
    def __init__(self):
        self.received = []
        self.closed = False
        self.failure = None

    def receive_uart(self, data):
        if self.failure is not None:
            self.received.append(data[:1])
            raise self.failure
        self.received.append(data)

    def close(self):
        self.closed = True


def connected(**options):
    context = Context()
    net = UartNet(context, **options)
    peers = (Peer(), Peer())
    handles = tuple(net.attach_port(peer, descriptor=PortSpec('uart', 'uart'),
        endpoint_name=f'{name}.uart', owner_port='uart', context=context)
        for name, peer in zip(('left', 'right'), peers))
    net.reset(Session(1))
    return net, context, peers, handles


def test_binary_chunks_keep_order_floor_and_upper_bound_provenance():
    net, context, (left, right), (a, b) = connected(delay_ns=2)
    context.t_sim_ns = context.not_before_ns = 10
    a.send(b'\x00\x7f', source_time_ns=901, mapped_time_ns=3, quality='upper_bound')
    a.send(b'\x80\xff', source_time_ns=902, mapped_time_ns=4, quality='upper_bound')
    b.send(b'back', mapped_time_ns=10)
    assert net.next_due_ns == 12 and net.pending_bytes == 8
    assert net.dispatch(11, 10) == 0
    assert net.dispatch(12, 2) == 2
    assert right.received == [b'\x00\x7f', b'\x80\xff'] and left.received == []
    assert net.dispatch(12, 2) == 1 and left.received == [b'back']
    assert net.pending == net.pending_bytes == 0
    rows = [row for row in context.events if row[1] == 'net.uart.delivery']
    assert rows[0][2] == {'source': 'left.uart', 'destination': 'right.uart',
        'data': b'\x00\x7f', 'sequence': 0, 'due_ns': 12}
    assert rows[0][3] == {'source_time_ns': 901, 'mapped_time_ns': 3,
        'delivery_time_ns': 12, 'quality': 'upper_bound'}


@pytest.mark.parametrize('options,chunks', [
    ({'max_pending': 1}, [b'a', b'b']),
    ({'max_pending_bytes': 3}, [b'ab', b'cd']),
])
def test_queue_exhaustion_freezes_before_new_chunk_or_sequence(options, chunks):
    net, context, peers, (handle, _) = connected(**options)
    handle.send(chunks[0])
    with pytest.raises(BufferError, match='capacity'):
        handle.send(chunks[1])
    assert net.pending == 1 and net.pending_bytes == len(chunks[0])
    assert len(context.events) == 1
    with pytest.raises(RuntimeError, match='healthy'):
        net.dispatch(0, 10)
    net.reset(Session(2))
    handle.send(b'z')
    net.dispatch(0, 10)
    assert peers[1].received == [b'z']


def test_default_byte_bound_and_empty_noop_are_independent_of_chunk_bound():
    net, context, _, (handle, _) = connected()
    handle.send(b'')
    assert context.events == []
    for _ in range(16):
        handle.send(b'x' * 4096)
    assert (net.pending, net.pending_bytes) == (16, 65536)
    with pytest.raises(BufferError):
        handle.send(b'!')


def test_invalid_payloads_reject_before_queue_effects_and_order_cannot_reverse():
    net, context, _, (handle, _) = connected()
    for invalid, error in [(bytearray(b'x'), TypeError), ('text', TypeError),
                           (b'x' * 4097, ValueError)]:
        with pytest.raises(error):
            handle.send(invalid)
    assert net.pending == 0 and context.events == []
    handle.send(b'first', mapped_time_ns=10)
    with pytest.raises(ValueError, match='decrease'):
        handle.send(b'older', mapped_time_ns=9)
    assert net.pending == 1


def test_sink_partial_failure_is_not_replayed_and_close_never_closes_borrow():
    net, _, peers, (handle, _) = connected()
    error = OSError('sink wrote prefix then failed')

    # Isolate the endpoint's IO; the net queue and error lifecycle stay real.
    peers[1].failure = error
    handle.send(b'abc')
    with pytest.raises(OSError) as caught:
        net.dispatch(0, 10)
    assert caught.value is error and peers[1].received == [b'a']
    with pytest.raises(RuntimeError):
        net.dispatch(0, 10)
    assert peers[1].received == [b'a'] and net.pending == 0
    net.close()
    net.close()
    assert not any(peer.closed for peer in peers)


@pytest.mark.parametrize('boundary', ['receiver', 'sent', 'delivery', 'replacement'])
def test_caught_callback_overflow_stops_active_io_and_preserves_error(boundary):
    net, context, peers, (handle, _) = connected(max_pending=1)
    errors = []

    def overflow():
        # Keep the first queued response; a rejected second response freezes it.
        if boundary != 'sent':
            handle.send(b'queued')
        try:
            handle.send(b'rejected')
        except BufferError as error:
            errors.append(error)

    if boundary in ('receiver', 'replacement'):
        def receive(data):
            peers[1].received.append(data)
            overflow()
            if boundary == 'replacement':
                raise OSError('later receiver error')
        net._endpoints['right.uart'] = (peers[1], receive)
    else:
        emit = context.emit
        def observe(port, kind, payload, **stamp):
            emit(port, kind, payload, **stamp)
            if kind == f'net.uart.{boundary}':
                overflow()
        context.emit = observe
    if boundary == 'sent':
        with pytest.raises(BufferError) as caught:
            handle.send(b'first')
        assert peers[1].received == []
    else:
        handle.send(b'first')
        with pytest.raises(BufferError) as caught:
            net.dispatch(0, 10)
        assert peers[1].received == [b'first']
    assert caught.value is errors[0] and net.pending == 1
    with pytest.raises(BufferError) as retained:
        _ = net.next_due_ns
    assert retained.value is errors[0]
    with pytest.raises(RuntimeError):
        net.dispatch(1, 10)
    assert len(peers[1].received) == (0 if boundary == 'sent' else 1)


def topology(count=2, direction='inout', **options):
    nodes = (NodeSpec('wire', 'net.uart', options),) + tuple(
        NodeSpec(f'peer{i}', 'fixture.peer') for i in range(count))
    links = tuple(LinkSpec(f'peer{i}.uart', 'wire.uart') for i in range(count))
    registry = {'net.uart': UartNetFactory(), 'fixture.peer': SimpleNamespace(
        describe=lambda _: NodeDescription((PortSpec('uart', 'uart', direction),)),
        create=lambda *_: pytest.fail('pure admission acquired resources'))}
    return Topology(nodes, links), registry


@pytest.mark.parametrize('count', [1, 3])
def test_exact_two_endpoint_admission_precedes_resources(count):
    graph, registry = topology(count)
    with pytest.raises(ValueError, match='connections|capacity'):
        Simulation(graph, registry)


def test_endpoint_directions_and_options_are_admitted_without_io():
    graph, registry = topology(direction='in')
    with pytest.raises(ValueError, match='inout UART'):
        Simulation(graph, registry)
    graph, registry = topology(unknown=True)
    with pytest.raises(ValueError, match='unknown UART'):
        Simulation(graph, registry)


class Source(Peer):
    def __init__(self, context):
        super().__init__()
        self.connection = context.attach('uart', self)

    def start(self):
        self.connection.send(b'\x00\xffbinary')


def test_python_echo_runs_at_same_cut_through_generic_settle_and_resets():
    graph = Topology((NodeSpec('wire', 'net.uart'), NodeSpec('source', 'source'),
                      NodeSpec('echo', 'device.uart-echo')),
        (LinkSpec('source.uart', 'wire.uart'), LinkSpec('echo.uart', 'wire.uart')))
    source = SimpleNamespace(describe=lambda _: NodeDescription((PortSpec('uart', 'uart'),)),
                             create=lambda _, ctx: Source(ctx))
    with Simulation(graph, {'net.uart': UartNetFactory(), 'source': source,
                           'device.uart-echo': UartEchoFactory()}) as sim:
        assert sim.components['source'].received == [b'\x00\xffbinary']
        deliveries = [event for event in sim.events if event.kind == 'net.uart.delivery']
        assert len(deliveries) == 2
        assert all(event.delivery_time_ns == 0 for event in deliveries)
        old = sim.components['source']
        sim.reset()
        assert old.closed and sim.components['source'].received == [b'\x00\xffbinary']


def test_two_echoes_cannot_exceed_generic_settle_budget():
    class Loop(Source):
        def receive_uart(self, data):
            self.connection.send(data)

    graph = Topology((NodeSpec('wire', 'net.uart', {'dispatch_budget': 4}),
                      NodeSpec('source', 'source'), NodeSpec('echo', 'device.uart-echo')),
        (LinkSpec('source.uart', 'wire.uart'), LinkSpec('echo.uart', 'wire.uart')))
    source = SimpleNamespace(describe=lambda _: NodeDescription((PortSpec('uart', 'uart'),)),
                             create=lambda _, ctx: Loop(ctx))
    sim = Simulation(graph, {'net.uart': UartNetFactory(), 'source': source,
                            'device.uart-echo': UartEchoFactory()})
    with pytest.raises(RuntimeError, match='dispatch budget exhausted'):
        sim.start()
    assert sim.state == SimulationState.FAILED and sim.components == {}
