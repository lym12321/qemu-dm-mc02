"""Group preflight and actual host child failure use the sole Simulation owner."""

import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sim.api import NodeDescription, NodeSpec, StepResult, Topology
from sim.apps.processes import HostProcessesFactory
from sim.apps.processes import admit_commands
from sim.config.yaml import ManifestError
from sim.runtime import Simulation, SimulationState


def test_all_machine_preflight_precedes_first_native_request():
    actions = []

    class Machine:
        def __init__(self, spec):
            self.name = spec.name

        def prepare_step(self, context):
            actions.append(('prepare', self.name))
            if self.name == 'second':
                raise RuntimeError('peer target changed')

        def request_step(self, context):
            actions.append(('request', self.name))

        def finish_step(self, context):
            return StepResult('ok', context.t_sim_ns)

        def close(self):
            actions.append(('close', self.name))

    factory = SimpleNamespace(describe=lambda _: NodeDescription(capabilities={'machine'}),
                              create=lambda spec, _: Machine(spec))
    with Simulation(Topology(tuple(NodeSpec(name, 'machine') for name in ('first', 'second'))),
                    {'machine': factory}) as sim:
        with pytest.raises(RuntimeError, match='peer target changed'):
            sim.step()
        assert actions == [('prepare', 'first'), ('prepare', 'second')]
        assert sim.t_sim_ns == sim.step_id == 0 and sim.state == SimulationState.FAILED


def process_simulation(tmp_path, *, allow=True):
    node = NodeSpec('host', 'app.host-processes', {'commands': {
        'upper': {'command': [sys.executable, '-c', 'import signal; signal.pause()']}}},
        base_dir=tmp_path)
    return Simulation(Topology((node,)), {
        'app.host-processes': HostProcessesFactory(allow_launch=allow)})


def test_actual_child_early_exit_freezes_before_next_cut_and_reaps(tmp_path):
    sim = process_simulation(tmp_path)
    sim.start()
    process = sim.components['host'].children[0].process
    try:
        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
        with pytest.raises(RuntimeError, match='host process upper exited'):
            sim.step()
        assert sim.state == SimulationState.FAILED and sim.t_sim_ns == 0
    finally:
        sim.close()
    assert process.poll() is not None and process.stdout.closed and process.stderr.closed


def test_process_factory_requires_opt_in_but_description_acquires_nothing(tmp_path, monkeypatch):
    sim = process_simulation(tmp_path, allow=False)
    monkeypatch.setattr('sim.apps.processes.subprocess.Popen',
                        lambda *a, **kw: pytest.fail('unexpected launch'))
    sim.validate()
    with pytest.raises(ValueError, match='explicit'):
        sim.start()
    assert sim.state == SimulationState.FAILED


@pytest.mark.parametrize('caught', [False, True])
def test_health_hook_reentry_freezes_and_cleanup_still_runs(caught):
    closed = []
    owner = {}

    def create(_, context):
        def health():
            if owner.get('reenter'):
                if caught:
                    try:
                        context.check_deadline()
                    except RuntimeError:
                        pass
                else:
                    context.check_deadline()
        return SimpleNamespace(check_health=health, close=lambda: closed.append(True))

    factory = SimpleNamespace(describe=lambda _: NodeDescription(capabilities={'health'}),
                              create=create)
    sim = Simulation(Topology((NodeSpec('host', 'fixture'),)), {'fixture': factory})
    sim.start()
    owner['reenter'] = True
    with pytest.raises(RuntimeError, match='health checks cannot reenter'):
        sim.step()
    assert sim.state == SimulationState.FAILED
    sim.close()
    assert closed == [True]


def test_health_checkers_are_recreated_on_reset_and_disabled_during_cleanup(tmp_path):
    sim = process_simulation(tmp_path)
    sim.start()
    old = sim.components['host'].children[0].process
    sim.reset()
    current = sim.components['host'].children[0].process
    assert old.poll() is not None and current.poll() is None
    sim.close()
    assert current.poll() is not None


def test_final_health_pass_refuses_exit_zero_then_closes_every_pipe(tmp_path, monkeypatch):
    sim = process_simulation(tmp_path)
    sim.start()
    process = sim.components['host'].children[0].process
    # Explicit unit fixture for the exit-zero branch, not firmware evidence.
    original = process.poll
    monkeypatch.setattr(process, 'poll', lambda: 0)
    with pytest.raises(RuntimeError, match=r'exited \(0\)'):
        sim.close()
    monkeypatch.setattr(process, 'poll', original)
    assert process.poll() is not None and process.stdout.closed and process.stderr.closed


def test_literal_arguments_and_child_cwd_path_are_admitted_without_launch(tmp_path, monkeypatch):
    executable = tmp_path / 'bin' / 'upper'
    executable.parent.mkdir()
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', 'bin')
    arguments = ['upper', '', 'two words', '$HOME', '$(touch unwanted)', '`touch unwanted`']
    command, = admit_commands({'observer': {'command': arguments}}, tmp_path)
    assert command.command == (str(executable), *arguments[1:])
    assert command.cwd == tmp_path
    assert not (tmp_path / 'unwanted').exists()


def test_admitted_python_preserves_virtual_environment_identity(tmp_path):
    command, = admit_commands({'observer': {'command': [
        sys.executable, '-c', 'import sys; print(sys.prefix)']}}, tmp_path)
    assert command.command[0] == os.path.abspath(sys.executable)
    result = subprocess.run(command.command, cwd=command.cwd, capture_output=True,
                            text=True, timeout=2, check=True)
    assert result.stdout.strip() == sys.prefix


def test_total_host_process_capacity_is_admitted_across_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr('sim.apps.processes.subprocess.Popen',
                        lambda *a, **kw: pytest.fail('admission launched a child'))
    options = {'commands': {f'child{i}': {'command': [sys.executable]} for i in range(4)}}
    factory = HostProcessesFactory(allow_launch=True)
    nodes = tuple(NodeSpec(name, 'host', options, base_dir=tmp_path) for name in ('a', 'b'))
    Simulation(Topology(nodes), {'host': factory}).validate()
    extra = NodeSpec('extra', 'host', {'commands': {'ninth': {'command': [sys.executable]}}},
                     base_dir=tmp_path)
    with pytest.raises(ValueError, match='at most 8'):
        Simulation(Topology((*nodes, extra)), {'host': factory})


@pytest.mark.parametrize('arguments', [[], [''], [sys.executable, True],
                                     [sys.executable, 'a\0b'], [sys.executable] + [''] * 128])
def test_invalid_host_arguments_are_rejected_before_resources(tmp_path, arguments):
    with pytest.raises(ManifestError):
        admit_commands({'observer': {'command': arguments}}, tmp_path)


def test_pipe_prepare_failure_reaps_newly_registered_process(tmp_path, monkeypatch):
    from sim.apps import processes

    launched, popen = [], processes.subprocess.Popen
    primary = OSError('pipe preparation failed')

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(processes.subprocess, 'Popen', record)
    monkeypatch.setattr(processes.ChildProcess, 'prepare', lambda _: (_ for _ in ()).throw(primary))
    sim = process_simulation(tmp_path)
    with pytest.raises(OSError) as caught:
        sim.start()
    assert caught.value is sim.failure is primary
    process, = launched
    assert process.poll() is not None and process.stdout.closed and process.stderr.closed
    sim.close()
