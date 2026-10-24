"""Explicit Python event source: four edges survive one 1 ms interval.

Run with an installed core: python examples/edge-events/main.py
These scripted events are not observations of a QEMU GPIO or UART waveform.
"""

import json

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.fabric.edge import DigitalEdge, DigitalEdgeNetFactory
from sim.runtime import Simulation


class ScriptedSource:
    def __init__(self, context):
        self.context = context
        self.output = context.attach('out', self)
        self.sent = False

    def step(self, ctx):
        if not self.sent:
            for mapped, level in ((100, 1), (101, 0), (101, 1), (999, 0)):
                self.output.send(DigitalEdge(level, 5000 + mapped, mapped))
            self.sent = True
        return StepResult('ok', ctx.t_sim_ns)

    def close(self):
        pass


class Receiver:
    def __init__(self, context):
        self.context = context
        self.received = []
        context.attach('in', self)

    def receive_edge(self, edge):
        self.received.append((edge.level, edge.source_time_ns,
                              edge.mapped_time_ns, self.context.t_sim_ns))

    def close(self):
        pass


class SourceFactory:
    def describe(self, spec):
        return NodeDescription((PortSpec('out', 'digital-edge', 'out', 'signal',
                                        capabilities={'event-output'}),), {'step'})

    def create(self, spec, context):
        return ScriptedSource(context)


class ReceiverFactory:
    def describe(self, spec):
        return NodeDescription((PortSpec('in', 'digital-edge', 'in', 'signal'),))

    def create(self, spec, context):
        return Receiver(context)


def simulation(**kwargs):
    topology = Topology((NodeSpec('source', 'source'), NodeSpec('wire', 'net.digital-edge'),
                         NodeSpec('sink', 'sink')),
                        (LinkSpec('source.out', 'wire.signal'), LinkSpec('sink.in', 'wire.signal')))
    return Simulation(topology, {'source': SourceFactory(), 'sink': ReceiverFactory(),
                                 'net.digital-edge': DigitalEdgeNetFactory()}, **kwargs)


def main():
    with simulation() as sim:
        sim.step()
        received = sim.components['sink'].received
    expected = [(1, 5100, 100, 1_000_000), (0, 5101, 101, 1_000_000),
                (1, 5101, 101, 1_000_000), (0, 5999, 999, 1_000_000)]
    assert received == expected
    print(json.dumps({'status': 'passed', 'source': 'scripted-python-events', 'edges': received}))


if __name__ == '__main__':
    main()
