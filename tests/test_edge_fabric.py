"""Event-source evidence, stable cuts, bounded delivery and no snapshot inference."""

from pathlib import Path
import runpy

import pytest

from sim.api import LinkSpec, NodeSpec, Topology
from sim.fabric.edge import DigitalEdge, DigitalEdgeNetFactory
from sim.runtime import Simulation, SimulationState


DEMO = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'examples/edge-events/main.py'))


def test_all_interval_edges_survive_with_distinct_source_mapping_and_delivery():
    with DEMO['simulation']() as sim:
        sim.step()
        assert sim.components['sink'].received == [
            (1, 5100, 100, 1_000_000), (0, 5101, 101, 1_000_000),
            (1, 5101, 101, 1_000_000), (0, 5999, 999, 1_000_000)]
        events = [e for e in sim.events if e.kind == 'net.digital-edge.delivery']
        assert [(e.payload['level'], e.payload['sequence'], e.source_time_ns,
                 e.mapped_time_ns, e.delivery_time_ns, e.quality) for e in events] == [
            (1, 0, 5100, 100, 1_000_000, 'exact'),
            (0, 1, 5101, 101, 1_000_000, 'exact'),
            (1, 2, 5101, 101, 1_000_000, 'exact'),
            (0, 3, 5999, 999, 1_000_000, 'exact')]
        sim.step()
        assert len(sim.components['sink'].received) == 4


def test_known_future_edges_cut_execution_without_coalescing_or_time_reversal():
    with DEMO['simulation'](quantum_ns=1000) as sim:
        source = sim.components['source']
        source.sent = True
        source.output.send(DigitalEdge(1, 5003, 3))
        source.output.send(DigitalEdge(0, 5004, 4))
        result = sim.run_until(10)
        assert result.diagnostics['substeps_completed'] == 3
        assert sim.components['sink'].received == [(1, 5003, 3, 3), (0, 5004, 4, 4)]
        sim.run_until(10)
        assert len(sim.components['sink'].received) == 2


@pytest.mark.parametrize('failure', ['capacity', 'receiver', 'backwards'])
def test_failure_freezes_without_replaying_delivered_or_pending_edges(failure):
    base = DEMO['simulation']()
    registry = dict(base.registry)
    if failure == 'receiver':
        class FailingReceiver(DEMO['Receiver']):
            def receive_edge(self, edge):
                self.received.append(edge.level)
                raise OSError('sink failed after effect')

        class FailingFactory(DEMO['ReceiverFactory']):
            def create(self, spec, context):
                return FailingReceiver(context)

        registry['sink'] = FailingFactory()
    with Simulation(base.topology, registry) as sim:
        source, sink, wire = (sim.components[n] for n in ('source', 'sink', 'wire'))
        source.sent = True
        if failure == 'capacity':
            wire.max_pending = 1
            source.output.send(DigitalEdge(1, 5000, 0))
            with pytest.raises(BufferError):
                source.output.send(DigitalEdge(0, 5001, 1))
        elif failure == 'backwards':
            source.output.send(DigitalEdge(1, 5001, 1))
            with pytest.raises(ValueError, match='not decrease'):
                source.output.send(DigitalEdge(0, 5000, 2))
        else:
            source.output.send(DigitalEdge(1, 5000, 0))
        with pytest.raises((BufferError, ValueError, OSError)):
            sim.run_until(1)
        assert sim.state == SimulationState.FAILED
        assert sim.t_sim_ns == 0
        assert sink.received == ([1] if failure == 'receiver' else [])
        with pytest.raises(RuntimeError):
            sim.step()


def test_reset_reconstructs_owners_and_invalidates_old_output_handle():
    with DEMO['simulation']() as sim:
        source = sim.components['source']
        wire = sim.components['wire']
        source.output.send(DigitalEdge(1, 5000, 100))
        sim.reset()
        assert wire.pending == 0
        with pytest.raises(RuntimeError, match='closed session'):
            source.output.send(DigitalEdge(0, 5001, 101))
        sim.step()
        assert len(sim.components['sink'].received) == 4


@pytest.mark.parametrize('boundary', ['receiver', 'observer', 'replacement'])
def test_caught_callback_overflow_stops_delivery_and_freezes_original_error(boundary):
    with DEMO['simulation']() as sim:
        source, sink, wire = (sim.components[name] for name in ('source', 'sink', 'wire'))
        source.sent = True
        wire.max_pending = 1
        errors = []

        def overflow():
            source.output.send(DigitalEdge(0, 5000, 0))
            try:
                source.output.send(DigitalEdge(1, 5000, 0))
            except BufferError as error:
                errors.append(error)

        if boundary in ('receiver', 'replacement'):
            receive = sink.receive_edge

            def receive_then_overflow(edge):
                receive(edge)
                overflow()
                if boundary == 'replacement':
                    raise OSError('later receiver error')

            wire._sinks[0] = ('sink.in', receive_then_overflow)
        else:
            sim.on_event = lambda event: overflow() if event.kind == 'net.digital-edge.delivery' else None
        source.output.send(DigitalEdge(1, 5000, 0))
        with pytest.raises(BufferError) as caught:
            sim.run_until(10)
        assert caught.value is errors[0]
        assert sink.received == [(1, 5000, 0, 0)]
        assert wire.pending == 1
        assert sim.state == SimulationState.FAILED and sim.t_sim_ns == 0
        with pytest.raises(RuntimeError):
            sim.run_until(10)
        assert len(sink.received) == 1


def test_due_edges_created_by_receiver_are_bounded_without_replay():
    base = DEMO['simulation']()
    registry = dict(base.registry)

    class FeedbackReceiver(DEMO['Receiver']):
        def receive_edge(self, edge):
            super().receive_edge(edge)
            self.send(DigitalEdge(1 - edge.level, edge.source_time_ns, edge.mapped_time_ns))

    class FeedbackFactory(DEMO['ReceiverFactory']):
        def create(self, spec, context):
            return FeedbackReceiver(context)

    registry['sink'] = FeedbackFactory()
    nodes = tuple(NodeSpec(n.name, n.kind, {'dispatch_budget': 4})
                  if n.name == 'wire' else n for n in base.topology.nodes)
    with Simulation(Topology(nodes, base.topology.links), registry) as sim:
        source, sink = (sim.components[name] for name in ('source', 'sink'))
        source.sent = True
        sink.send = source.output.send
        source.output.send(DigitalEdge(1, 5000, 0))
        with pytest.raises(RuntimeError, match='dispatch budget exhausted'):
            sim.step()
        assert sink.received == [(1, 5000, 0, 0), (0, 5000, 0, 0),
                                 (1, 5000, 0, 0), (0, 5000, 0, 0)]
        assert sim.state == SimulationState.FAILED and sim.t_sim_ns == 0
        with pytest.raises(RuntimeError):
            sim.step()
        assert len(sink.received) == 4


def test_incompatible_sampled_ports_are_rejected_before_construction(tmp_path):
    from sim.composition import builtin_registry
    elf = tmp_path / 'firmware.elf'
    elf.write_bytes(b'ELF fixture for pure admission')
    registry = builtin_registry()
    registry['net.digital-edge'] = DigitalEdgeNetFactory()
    registry['sink'] = DEMO['ReceiverFactory']()
    topology = Topology((NodeSpec('m', 'machine.qemu', {'firmware': elf}),
                         NodeSpec('wire', 'net.digital-edge'), NodeSpec('sink', 'sink')),
                        (LinkSpec('m.gpio.B0.out', 'wire.signal'), LinkSpec('sink.in', 'wire.signal')))
    with pytest.raises(ValueError, match='protocol'):
        Simulation(topology, registry)


@pytest.mark.parametrize('level,source,mapped', [(True, 0, 0), (2, 0, 0), (0, -1, 0), (0, 0, 1 << 64)])
def test_edge_admission(level, source, mapped):
    with pytest.raises(ValueError):
        DigitalEdge(level, source, mapped)
