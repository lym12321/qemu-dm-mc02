"""Public CAN launcher admission must fail before opening nonexistent sockets."""

from pathlib import Path
import subprocess
import sys

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / 'tools/run-can-worker.py'


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
