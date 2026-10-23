"""Direct generic-Simulation consumer of completed SI IMU connections."""

import pytest

from sim.api import ImuSample, ImuSignalSample, LinkSpec, NodeDescription, NodeSpec, PortSpec, StepResult, Topology
from sim.devices.bmi088 import imu_to_bmi088_units
from sim.fabric.imu import ImuNetFactory
from sim.runtime import Simulation, SimulationState


LEFT = ImuSample((0.0, 0.0, 1.0), (0.0, 0.0, 9.80665))
RIGHT = ImuSample((0.0, 0.0, -1.0), (0.0, 0.0, -9.80665))


def fixture_simulation(actions, *, failure=None):
    class Fixture:
        def __init__(self, spec, context):
            self.name, self.context = spec.name, context
            self.value = LEFT if self.name == 'left' else RIGHT
            self.completed = ImuSignalSample(self.value, None, 0)
            self.received = None
            context.attach('out', self)
            context.attach('in', self)

        def step(self, step):
            actions.append(('step', self.name, step.t_sim_ns, self.received.sample))
            self.completed = ImuSignalSample(self.value, 500 + step.t_sim_ns, step.t_sim_ns)
            return StepResult('ok', step.t_sim_ns)

        def read_imu(self):
            actions.append(('sample', self.name, self.completed.mapped_time_ns))
            if failure == 'source' and self.name == 'right' and self.context.t_sim_ns == 7:
                raise OSError('source failed at completed cut')
            return self.completed

        def receive_imu(self, sample):
            if failure == 'sink' and self.name == 'left' and self.context.t_sim_ns == 7:
                raise OSError('sink failed at completed cut')
            actions.append(('receive', self.name, self.context.t_sim_ns, sample.sample))
            self.received = sample
            self.units = imu_to_bmi088_units(sample.sample)

        def close(self):
            actions.append(('close', self.name))

    class Factory:
        def describe(self, _spec):
            return NodeDescription((PortSpec('out', 'imu', 'out', 'signal'),
                                    PortSpec('in', 'imu', 'in', 'signal')), {'step'})

        def create(self, spec, context):
            return Fixture(spec, context)

    graph = Topology((NodeSpec('left', 'fixture'), NodeSpec('right', 'fixture'),
                      NodeSpec('left_wire', 'net.imu'), NodeSpec('right_wire', 'net.imu')),
                     (LinkSpec('left.out', 'left_wire.signal'),
                      LinkSpec('left_wire.signal', 'right.in'),
                      LinkSpec('right.out', 'right_wire.signal'),
                      LinkSpec('right_wire.signal', 'left.in')))
    return Simulation(graph, {'fixture': Factory(), 'net.imu': ImuNetFactory()}, quantum_ns=10)


def test_all_sources_sample_before_any_sink_write_at_initial_and_nongrid_completed_cuts():
    actions = []
    with fixture_simulation(actions) as simulation:
        assert actions == [('sample', 'left', 0), ('sample', 'right', 0),
                           ('receive', 'right', 0, LEFT), ('receive', 'left', 0, RIGHT)]
        assert simulation.components['right'].units[1] == (0.0, 0.0, 1.0)
        assert simulation.components['left'].units[1] == (0.0, 0.0, -1.0)
        for duration, cut in ((7, 7), (3, 10)):
            actions.clear()
            simulation.step(duration)
            assert actions == [('step', 'left', cut, RIGHT), ('step', 'right', cut, LEFT),
                               ('sample', 'left', cut), ('sample', 'right', cut),
                               ('receive', 'right', cut, LEFT), ('receive', 'left', cut, RIGHT)]
            actions.clear()
            simulation.run_until(cut)
            assert actions == []  # A completed cut is never resampled or rebroadcast.
        deliveries = [event for event in simulation.events if event.kind == 'net.imu.delivery']
        assert [event.mapped_time_ns for event in deliveries] == [0, 0, 7, 7, 10, 10]
        assert [event.source_time_ns for event in deliveries] == [None, None, 507, 507, 510, 510]
        assert all(event.delivery_time_ns == event.mapped_time_ns and event.quality == 'sampled'
                   for event in deliveries)


def test_reset_recreates_owned_instances_and_delivers_a_new_initial_sample_once():
    actions = []
    with fixture_simulation(actions) as simulation:
        old = dict(simulation.components)
        simulation.step(7)
        actions.clear()
        simulation.reset()
        assert actions == [('close', 'right'), ('close', 'left'),
                           ('sample', 'left', 0), ('sample', 'right', 0),
                           ('receive', 'right', 0, LEFT), ('receive', 'left', 0, RIGHT)]
        assert simulation.session.session_id == 2 and simulation.t_sim_ns == 0
        assert all(simulation.components[name] is not component for name, component in old.items())
        for name in ('left_wire', 'right_wire'):
            assert old[name].pending == 0
            with pytest.raises(RuntimeError, match='healthy'):
                old[name].dispatch(7, 1)
        deliveries = [event for event in simulation.events
                      if event.session_id == 2 and event.kind == 'net.imu.delivery']
        assert len(deliveries) == 2
        assert all(event.payload['sequence'] == 0 for event in deliveries)


@pytest.mark.parametrize('failure', ['source', 'sink'])
def test_failed_cut_preserves_partial_effects_and_cannot_step_reset_or_replay(failure):
    actions = []
    with fixture_simulation(actions, failure=failure) as simulation:
        actions.clear()
        with pytest.raises(OSError, match=f'{failure} failed') as caught:
            simulation.step(7)
        assert simulation.failure is caught.value and simulation.state == SimulationState.FAILED
        assert simulation.t_sim_ns == 0
        deliveries = [action for action in actions if action[0] == 'receive']
        assert deliveries == ([] if failure == 'source' else [('receive', 'right', 7, LEFT)])
        before = list(actions)
        for operation in (lambda: simulation.step(7), simulation.reset):
            with pytest.raises(RuntimeError, match='healthy'):
                operation()
        assert actions == before
