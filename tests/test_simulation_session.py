"""Explicit session origin and borrowed host deadlines share the same clock."""
import math

import pytest

from sim.api import NodeDescription, NodeSpec, Session, StepContext, StepResult, Topology
from sim.runtime import Simulation
from sim.runtime import simulation as runtime


class Factory:
    def __init__(self):
        self.instances = []

    def describe(self, spec):
        return NodeDescription(capabilities=frozenset({'step'}))

    def create(self, spec, context):
        owner = Owner(context)
        self.instances.append(owner)
        return owner


class Owner:
    def __init__(self, context):
        self.context = context
        self.sessions = []
        self.steps = []
        self.closed = False
        self.on_step = None

    def reset(self, session):
        self.sessions.append(session)
        assert self.context.t_sim_ns == session.t_sim_ns
        assert self.context.not_before_ns == session.t_sim_ns

    def step(self, context):
        self.steps.append(context)
        if self.on_step:
            self.on_step()
        return StepResult('ok', context.t_sim_ns)

    def close(self):
        self.closed = True


def create(**options):
    factory = Factory()
    return Simulation(Topology((NodeSpec('world', 'fixture'),)),
                      {'fixture': factory}, **options), factory


def test_session_identity_origin_and_recreated_reset():
    session = Session(71, 500)
    simulation, factory = create(initial_session=session, quantum_ns=100)
    try:
        assert not factory.instances and simulation.session is None
        simulation.start()
        first = factory.instances[0]
        assert simulation.session is first.sessions[0] is session
        assert simulation.t_sim_ns == 500 and simulation.step_id == 0
        simulation.step(100)
        assert first.steps == [StepContext(71, 1, 600, 100)]
        simulation.reset()
        assert first.closed and len(factory.instances) == 2
        assert simulation.session == Session(72, 500)
        assert simulation.t_sim_ns == 500 and simulation.step_id == 0
        simulation.step(1)
        assert factory.instances[1].steps == [StepContext(72, 1, 501, 1)]
    finally:
        simulation.close()


def test_origin_admission_and_uint64_overflow_precede_effects():
    with pytest.raises(TypeError, match='initial_session'):
        create(initial_session={'session_id': 2})
    simulation, factory = create(initial_session=Session(3, 0xffffffffffffffff))
    try:
        simulation.start()
        with pytest.raises(ValueError):
            simulation.step(1)
        assert not factory.instances[0].steps and simulation.state == 'paused'
    finally:
        simulation.close()


def test_borrowed_deadline_replaces_default_caps_and_requires_each_operation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(runtime, 'monotonic', lambda: now[0])
    simulation, factory = create(startup_timeout_s=.01, host_timeout_s=.02)
    try:
        simulation.start(deadline_monotonic=200)
        owner = factory.instances[0]
        assert owner.context.deadline_monotonic == 200
        now[0] = 150
        for operation in (lambda: simulation.step(1), lambda: simulation.run_until(1), simulation.reset):
            with pytest.raises(ValueError, match='each operation'):
                operation()
        for invalid in (True, 0, -1, math.nan, math.inf, 10 ** 1000):
            with pytest.raises(ValueError, match='finite positive'):
                simulation.step(1, deadline_monotonic=invalid)
        assert simulation.state == 'paused' and not owner.steps
        simulation.step(1, deadline_monotonic=250)
        simulation.reset(deadline_monotonic=300)
        assert owner.closed and factory.instances[1].context.deadline_monotonic == 300
        now[0] = 299
        simulation.run_until(1, deadline_monotonic=301)
        assert simulation.t_sim_ns == 1
    finally:
        simulation.close()


def test_expired_borrowed_deadline_freezes_before_next_effect(monkeypatch):
    monkeypatch.setattr(runtime, 'monotonic', lambda: 100.0)
    simulation, factory = create()
    simulation.start(deadline_monotonic=101)
    try:
        with pytest.raises(TimeoutError, match='deadline'):
            simulation.step(1, deadline_monotonic=99)
        assert simulation.state == 'failed' and not factory.instances[0].steps
    finally:
        simulation.close()
    simulation, factory = create()
    try:
        with pytest.raises(TimeoutError, match='deadline'):
            simulation.start(deadline_monotonic=99)
        assert simulation.state == 'failed' and not factory.instances
    finally:
        simulation.close()


def test_default_budget_cannot_be_extended_or_borrowed_during_reentry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(runtime, 'monotonic', lambda: now[0])
    simulation, factory = create(host_timeout_s=2)
    try:
        simulation.start()
        with pytest.raises(ValueError, match='require start'):
            simulation.step(1, deadline_monotonic=200)
        owner = factory.instances[0]
        def reenter():
            with pytest.raises(RuntimeError, match='paused'):
                simulation.step(1, deadline_monotonic=300)
        owner.on_step = reenter
        simulation.step(1)
        assert owner.context.deadline_monotonic == 102
        now[0] = 103
        with pytest.raises(TimeoutError):
            simulation.step(1)
        assert len(owner.steps) == 1
    finally:
        simulation.close()
