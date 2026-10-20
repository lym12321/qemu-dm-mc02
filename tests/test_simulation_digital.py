"""Direct generic-Simulation consumer of sampled digital connection owners."""

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.fabric.digital import DigitalNetFactory, DigitalSample
from sim.runtime import Simulation


def test_all_sources_sample_before_any_sink_write_and_inputs_wait_for_next_interval():
    actions = []

    class Fixture:
        def __init__(self, spec, context):
            self.name, self.context = spec.name, context
            self.level, self.received = spec.options['level'], None
            self.completed = DigitalSample(None, 500, 0)
            context.attach('out', self)
            context.attach('in', self)

        def step(self, step):
            actions.append(('step', self.name, step.t_sim_ns, self.received))
            self.completed = DigitalSample(self.level, 500 + step.t_sim_ns, step.t_sim_ns)
            return StepResult('ok', step.t_sim_ns)

        def read_digital(self):
            actions.append(('sample', self.name, self.completed.mapped_time_ns, self.completed.level))
            return self.completed

        def receive_digital(self, level):
            actions.append(('receive', self.name, self.context.t_sim_ns, level))
            self.received = level

        def close(self):
            pass

    class Factory:
        def describe(self, _spec):
            return NodeDescription((PortSpec('out', 'digital', 'out', 'signal'),
                                    PortSpec('in', 'digital', 'in', 'signal')), {'step'})

        def create(self, spec, context):
            return Fixture(spec, context)

    graph = Topology((NodeSpec('left', 'fixture', {'level': 0}),
                      NodeSpec('right', 'fixture', {'level': 1}),
                      NodeSpec('left_wire', 'net.digital'), NodeSpec('right_wire', 'net.digital')),
                     (LinkSpec('left.out', 'left_wire.signal'),
                      LinkSpec('left_wire.signal', 'right.in'),
                      LinkSpec('right.out', 'right_wire.signal'),
                      LinkSpec('right_wire.signal', 'left.in')))
    with Simulation(graph, {'fixture': Factory(), 'net.digital': DigitalNetFactory()},
                    quantum_ns=10) as simulation:
        assert actions == [('sample', 'left', 0, None), ('sample', 'right', 0, None)]
        assert not any(event.kind == 'net.digital.delivery' for event in simulation.events)
        actions.clear()
        simulation.step(10)
        assert actions == [('step', 'left', 10, None), ('step', 'right', 10, None),
                           ('sample', 'left', 10, 0), ('sample', 'right', 10, 1),
                           ('receive', 'right', 10, 0), ('receive', 'left', 10, 1)]
        actions.clear()
        simulation.run_until(10)
        assert actions == []  # Reentering a completed cut never resamples it.
        simulation.step(10)
        assert actions == [('step', 'left', 20, 1), ('step', 'right', 20, 0),
                           ('sample', 'left', 20, 0), ('sample', 'right', 20, 1)]
