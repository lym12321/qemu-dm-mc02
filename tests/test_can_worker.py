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
    (['--m3508-timeout-ms', '3600001'], 'at most 3600s'),
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


def track_admitted_plant(monkeypatch, *, cleanup_error=None):
    from sim.apps import four_wheel
    admitted, closes = [], []
    create = four_wheel.create_four_wheel_plant
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
    monkeypatch.setattr(four_wheel, 'create_four_wheel_plant', observed_create)
    return admitted, closes


def test_chassis_connection_failure_follows_completed_native_preflight(monkeypatch, capsys):
    pytest.importorskip('mujoco', reason='optional native chassis profile')
    launcher = chassis_launcher()
    admitted, closes = track_admitted_plant(monkeypatch)
    def failed_run(args, *, simulation_factory, imu_reader):
        assert callable(simulation_factory) and callable(imu_reader)
        assert len(admitted) == 1 and closes == admitted and admitted[0].closed
        raise ConnectionError('connect boundary')
    monkeypatch.setattr(launcher.worker, 'run', failed_run)
    assert launcher.main(['--cosim', '/missing/cosim', '--fdcan', '/missing/can']) == 1
    assert closes == admitted
    assert 'chassis worker: connect boundary' in capsys.readouterr().err


def test_chassis_failed_preflight_cleanup_prevents_connection(monkeypatch, capsys):
    pytest.importorskip('mujoco', reason='optional native chassis profile')
    launcher = chassis_launcher()
    admitted, closes = track_admitted_plant(monkeypatch, cleanup_error=OSError('close boundary'))
    monkeypatch.setattr(launcher.worker, 'run', lambda *a, **k: pytest.fail('connected'))
    assert launcher.main(['--cosim', '/missing/cosim', '--fdcan', '/missing/can']) == 2
    assert closes == admitted and admitted[0].closed
    assert 'chassis profile: close boundary' in capsys.readouterr().err


def test_chassis_preflight_recipe_rejects_changed_model_before_acquisition(tmp_path):
    pytest.importorskip('mujoco', reason='optional native chassis profile')
    from sim.apps.four_wheel import four_wheel_simulation_factory
    from sim.api import Session
    from sim.transports.cosim import QemuTimestampMapper
    model = tmp_path / 'model.xml'
    model.write_bytes((LAUNCHER.parents[1] / 'models/four_wheel.xml').read_bytes())
    acquire = four_wheel_simulation_factory(model)
    model.write_text(model.read_text().replace('mass=".4"', 'mass=".5"'))
    with pytest.raises(ValueError, match='changed after native preflight'):
        acquire(Session(1), QemuTimestampMapper(0, 0), object(), None)
