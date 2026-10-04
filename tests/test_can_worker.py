"""Public CAN launcher admission must fail before opening nonexistent sockets."""

from pathlib import Path
import importlib.util
import subprocess
import sys

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / 'tools/run-can-worker.py'
CHASSIS_LAUNCHER = LAUNCHER.with_name('run-chassis-worker.py')


@pytest.mark.parametrize('options,message', [
    (['--m3508-id', '1', '--m3508-id', '1'], 'must be unique'),
    (['--m3508-timeout-ms', '0'], 'positive unsigned64'),
    (['--can-delay-ns', '-1'], 'nonnegative'),
    (['--torque-per-amp', 'nan'], 'finite and nonnegative'),
])
def test_profile_admission_precedes_connection(tmp_path, options, message):
    result = subprocess.run(
        [sys.executable, '-I', str(LAUNCHER), '--cosim', '/missing/cosim',
         '--fdcan', '/missing/can', *options], cwd=tmp_path,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert 'CAN profile:' in result.stderr and message in result.stderr
    assert 'Traceback' not in result.stderr


def test_help_includes_worker_and_can_options_outside_repository(tmp_path):
    result = subprocess.run([sys.executable, '-I', str(LAUNCHER), '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    for option in ('--protocol', '--socketcan', '--wait-step-done', '--m3508-id',
                   '--m3508-timeout-ms', '--torque-per-amp'):
        assert option in result.stdout


@pytest.mark.parametrize('options,message', [
    (['--engine', 'mujoco', '--mujoco-model', '/unused/model.xml'], 'default engine'),
    (['--m3508-timeout-ms', '0'], 'positive unsigned64'),
    (['--torque-per-amp', 'nan'], 'finite and nonnegative'),
    (['--can-delay-ns', '-1'], 'nonnegative'),
    (['--chassis-model', '/missing/absent-chassis.xml'], 'absent-chassis.xml'),
])
def test_chassis_profile_admission_precedes_socket_connection(tmp_path, options, message):
    if '--engine' not in options:
        pytest.importorskip('mujoco', reason='optional native chassis profile')
    result = subprocess.run(
        [sys.executable, '-I', str(CHASSIS_LAUNCHER), '--cosim', '/missing/cosim',
         '--fdcan', '/missing/can', *options], cwd=tmp_path,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert 'chassis profile:' in result.stderr and message in result.stderr
    assert 'Traceback' not in result.stderr


def test_chassis_help_exposes_shared_profile_outside_repository(tmp_path):
    result = subprocess.run([sys.executable, '-I', str(CHASSIS_LAUNCHER), '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    for option in ('--protocol', '--fdcan', '--socketcan', '--wait-step-done',
                   '--chassis-model', '--m3508-timeout-ms', '--torque-per-amp'):
        assert option in result.stdout
    assert '--m3508-id' not in result.stdout


def chassis_launcher():
    spec = importlib.util.spec_from_file_location('chassis_launcher', CHASSIS_LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def track_admitted_plant(monkeypatch, launcher, *, cleanup_error=None):
    admitted, closes = [], []
    create = launcher.create_four_wheel_plant

    def observed_create(*args, **kwargs):
        plant = create(*args, **kwargs)
        admitted.append(plant)
        close = plant.close

        def observed_close():
            closes.append(plant)
            close()
            if cleanup_error is not None:
                raise cleanup_error

        monkeypatch.setattr(plant, 'close', observed_close)
        return plant

    monkeypatch.setattr(launcher, 'create_four_wheel_plant', observed_create)
    return admitted, closes


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_chassis_connection_failure_closes_prebuilt_profile_and_preserves_primary(
        monkeypatch, capsys, cleanup_fails):
    pytest.importorskip('mujoco', reason='optional native chassis profile')
    launcher = chassis_launcher()
    error = OSError('profile close boundary') if cleanup_fails else None
    admitted, closes = track_admitted_plant(monkeypatch, launcher, cleanup_error=error)
    primary = ConnectionError('connect boundary')

    def failed_run(args, *, plant_factory):
        assert callable(plant_factory) and len(admitted) == 1
        assert not admitted[0].closed and admitted[0].physics.time_s == 0
        raise primary

    monkeypatch.setattr(launcher.worker, 'run', failed_run)
    assert launcher.main(['--cosim', '/missing/cosim', '--fdcan', '/missing/can']) == 1
    assert closes == admitted and admitted[0].closed
    assert all(device.closed for device in admitted[0].devices)
    stderr = capsys.readouterr().err
    assert 'chassis worker: connect boundary' in stderr
    if cleanup_fails:
        assert 'chassis profile cleanup failed: profile close boundary' in stderr
        assert primary.__notes__ == ['chassis profile cleanup failed: profile close boundary']


def test_chassis_runtime_handoff_moves_cleanup_to_worker_once(monkeypatch):
    pytest.importorskip('mujoco', reason='optional native chassis profile')
    launcher = chassis_launcher()
    admitted, closes = track_admitted_plant(monkeypatch, launcher)

    class BorrowedCan:
        def send(self, *args):
            raise AssertionError('no transport output before runtime reset/step')

    def run(args, *, plant_factory):
        runtime = plant_factory(BorrowedCan(), None)
        assert runtime.plant is admitted[0]
        runtime.close()
        return 0

    monkeypatch.setattr(launcher.worker, 'run', run)
    assert launcher.main(['--cosim', '/missing/cosim', '--fdcan', '/missing/can']) == 0
    assert closes == admitted and admitted[0].closed
