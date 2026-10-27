"""CLI recording lifecycle and offline replay over the real experiment consumer."""

from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from sim import cli

ROOT = Path(__file__).resolve().parents[1]


def bus_scene(tmp_path, *, mode='stepped', count=1):
    scene = tmp_path / 'scene.yaml'
    scene.write_text(yaml.safe_dump({'schema': 'sim/v1',
        'runtime': {'mode': mode, 'quantum': '10ns', 'duration': '20ns'},
        'nodes': {f'can{index}': {'kind': 'bus.can'} for index in range(count)}}))
    return scene


@pytest.mark.parametrize('payload,code', [('OK', 0), ('BAD', 1)])
def test_recording_assertion_failure_is_complete_and_replay_needs_no_scene(
        tmp_path, monkeypatch, capsys, payload, code):
    pytest.importorskip('mcap')
    from test_scene_cli import _cli_experiment
    from sim import composition
    from sim.config import scene as scene_module

    scene, report, closed, _ = _cli_experiment(tmp_path, monkeypatch, payload=payload)
    trace = tmp_path / 'capture.mcap'
    assert cli.main(['run', str(scene), '--trace', str(trace), '--report', str(report)]) == code
    original = json.loads(report.read_text())
    assert closed == [True]
    raw = trace.read_bytes()
    assert raw.startswith(b'\x89MCAP0\r\n') and raw.endswith(b'\x89MCAP0\r\n')
    scene.unlink()
    monkeypatch.setattr(composition, 'builtin_registry', lambda **_: pytest.fail('offline registry'))
    monkeypatch.setattr(scene_module, 'load_scene', lambda *_: pytest.fail('offline scene'))
    replay_report = tmp_path / 'replay.json'
    assert cli.main(['replay', str(trace), '--report', str(replay_report)]) == code
    replay = json.loads(replay_report.read_text())
    assert replay['result'] == original['result']
    assert replay['provenance'] == {'mode': 'offline-replay', 'trace': str(trace), 'assertions': None}
    assert 'offline replay ' in capsys.readouterr().out
    assert trace.read_bytes() == raw


def test_recording_plain_scene_preserves_existing_time_report(tmp_path):
    pytest.importorskip('mcap')
    scene = bus_scene(tmp_path)
    trace, report = tmp_path / 'capture.mcap', tmp_path / 'run.json'
    assert cli.main(['run', str(scene), '--trace', str(trace), '--report', str(report)]) == 0
    assert json.loads(report.read_text()) == {'status': 'passed', 'result': {'time_ns': 20}}
    replay = tmp_path / 'replay.json'
    assert cli.main(['replay', str(trace), '--report', str(replay)]) == 0
    result = json.loads(replay.read_text())['result']
    assert result['end_ns'] == 20 and result['status'] == [] and result['matches'] == []


@pytest.mark.parametrize('problem', ['existing-trace', 'existing-report', 'same-output',
    'nested-output', 'symlink', 'hardlink', 'interactive', 'node-limit'])
def test_recording_output_and_mode_admission_precedes_component_acquisition(
        tmp_path, monkeypatch, problem):
    from sim import composition
    original = composition.builtin_registry

    def registry(**options):
        values = original(**options)
        for factory in values.values():
            monkeypatch.setattr(factory, 'create', lambda *_: pytest.fail('native acquisition'))
        return values

    monkeypatch.setattr(composition, 'builtin_registry', registry)
    scene = bus_scene(tmp_path, mode='interactive' if problem == 'interactive' else 'stepped',
                      count=64 if problem == 'node-limit' else 1)
    trace, report = tmp_path / 'capture.mcap', tmp_path / 'report.json'
    preserved = {}
    if problem in ('existing-trace', 'symlink', 'hardlink'):
        source = scene if problem != 'existing-trace' else trace
        if problem == 'existing-trace':
            trace.write_bytes(b'existing capture')
        elif problem == 'symlink':
            trace.symlink_to(source)
        else:
            import os
            os.link(source, trace)
        preserved[trace] = trace.read_bytes()
    elif problem == 'existing-report':
        report.write_bytes(b'existing report')
        preserved[report] = report.read_bytes()
    elif problem == 'same-output':
        report = trace
    elif problem == 'nested-output':
        report = trace / 'report.json'
    assert cli.main(['run', str(scene), '--trace', str(trace), '--report', str(report)]) == 1
    for path, raw in preserved.items():
        assert path.read_bytes() == raw
    if problem in ('interactive', 'node-limit', 'same-output', 'nested-output'):
        assert not trace.exists()


@pytest.mark.parametrize('document', [
    'assertions: []\nunknown: true\n',
    'assertions: []\nassertions: []\n',
    'assertions: &items []\n',
    'assertions: null\n',
    yaml.safe_dump({'assertions': [{}] * 1025}),
])
def test_replay_assertion_document_uses_bounded_unique_yaml_before_trace_io(tmp_path, document):
    assertions = tmp_path / 'assertions.yaml'
    assertions.write_text(document)
    report = tmp_path / 'report.json'
    assert cli.main(['replay', str(tmp_path / 'missing.mcap'), '--assertions', str(assertions),
                     '--report', str(report)]) == 1
    assert json.loads(report.read_text())['error_type'] == 'ManifestError'


def test_replay_rejects_fifo_before_a_blocking_open(tmp_path):
    import os

    if not hasattr(os, 'mkfifo'):
        pytest.skip('named pipes unavailable')
    source = tmp_path / 'pipe.mcap'
    os.mkfifo(source)
    report = tmp_path / 'report.json'
    assert cli.main(['replay', str(source), '--report', str(report)]) == 1
    value = json.loads(report.read_text())
    assert value['error'] == 'replay trace must be a regular file'
    assert value['provenance']['mode'] == 'offline-replay'


@pytest.mark.parametrize('input_kind', ['trace', 'assertions'])
@pytest.mark.parametrize('relation', ['same', 'input-ancestor', 'output-ancestor'])
def test_replay_rejects_missing_input_output_aliases_before_creating_a_report(
        tmp_path, input_kind, relation):
    source = tmp_path / 'missing-input'
    report = source if relation == 'same' else (
        source / 'report.json' if relation == 'input-ancestor' else tmp_path / 'parent')
    if relation == 'output-ancestor':
        source = report / 'source'
    trace = source if input_kind == 'trace' else tmp_path / 'missing.mcap'
    arguments = ['replay', str(trace), '--report', str(report)]
    if input_kind == 'assertions':
        arguments.extend(['--assertions', str(source)])
    assert cli.main(arguments) == 1
    assert not source.exists() and not report.exists()


def test_replay_replacement_assertions_use_recorded_endpoints_and_bounds(tmp_path, monkeypatch):
    pytest.importorskip('mcap')
    from test_scene_cli import _cli_experiment

    scene, report, _, _ = _cli_experiment(tmp_path, monkeypatch)
    trace = tmp_path / 'capture.mcap'
    assert cli.main(['run', str(scene), '--trace', str(trace), '--report', str(report)]) == 0
    original = trace.read_bytes()
    assertions = tmp_path / 'assertions.yaml'
    for name, condition, error_type in (
            ('fail', {'before': '10ns', 'uart_contains': {'endpoint': 'port.uart', 'text': 'MISSING'}}, None),
            ('endpoint', {'before': '10ns', 'uart_contains': {'endpoint': 'absent.uart', 'text': 'OK'}}, 'ValueError'),
            ('deadline', {'before': '21ns', 'uart_contains': {'endpoint': 'port.uart', 'text': 'OK'}}, 'ValueError')):
        assertions.write_text(yaml.safe_dump({'assertions': [condition]}))
        replay = tmp_path / f'{name}.json'
        assert cli.main(['replay', str(trace), '--assertions', str(assertions), '--report', str(replay)]) == 1
        value = json.loads(replay.read_text())
        assert value['status'] == 'failed'
        if error_type is None:
            assert value['result']['status'] == ['fail']
        else:
            assert value['error_type'] == error_type
        assert trace.read_bytes() == original


@pytest.mark.parametrize('route', ['run', 'replay'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_trace_runtime_failure_preserves_primary_and_independent_close_note(
        tmp_path, monkeypatch, route, interrupted):
    from sim.apps import experiment

    scene = bus_scene(tmp_path)
    trace, report = tmp_path / 'capture.mcap', tmp_path / 'report.json'
    primary = KeyboardInterrupt('record interrupted') if interrupted else OSError('record failed')
    if route == 'replay':
        trace.write_bytes(b'borrowed regular input')

    class Sink(BytesIO):
        def close(self):
            raise OSError('trace close failed')

    sink = Sink()
    original = Path.open

    def opened(path, *args, **kwargs):
        return sink if path == trace else original(path, *args, **kwargs)

    def failed(*args, **options):
        assert (options['trace'] if route == 'run' else args[0]) is sink
        raise primary

    monkeypatch.setattr(Path, 'open', opened)
    monkeypatch.setattr(experiment, 'run_experiment' if route == 'run' else 'replay_experiment', failed)
    arguments = ['run', str(scene), '--trace', str(trace)] if route == 'run' else ['replay', str(trace)]
    assert cli.main([*arguments, '--report', str(report)]) == (130 if interrupted else 1)
    value = json.loads(report.read_text())
    assert value['error'] == str(primary)
    label = 'trace' if route == 'run' else 'replay source'
    assert f'{label} cleanup: OSError: trace close failed' in value['notes']


def test_late_trace_close_failure_retains_physical_bytes_and_rejects_cli_success(
        tmp_path, monkeypatch, capsys):
    from sim.apps import experiment

    scene = bus_scene(tmp_path)
    trace, report = tmp_path / 'capture.mcap', tmp_path / 'report.json'

    class Sink(BytesIO):
        def close(self):
            raise OSError('late close failed')

    sink = Sink()
    original = Path.open
    monkeypatch.setattr(Path, 'open', lambda path, *args, **kwargs:
        sink if path == trace else original(path, *args, **kwargs))

    def completed(*args, **options):
        options['trace'].write(b'physically written transcript')
        return experiment.ExperimentResult((), (), (), 20, 0, 0)

    monkeypatch.setattr(experiment, 'run_experiment', completed)
    assert cli.main(['run', str(scene), '--trace', str(trace), '--report', str(report)]) == 1
    assert sink.getvalue() == b'physically written transcript'
    value = json.loads(report.read_text())
    assert value['status'] == 'failed' and value['error'] == 'late close failed'
    assert 'passed:' not in capsys.readouterr().out


def test_component_cleanup_failure_leaves_capture_incomplete_for_offline_replay(tmp_path, monkeypatch):
    pytest.importorskip('mcap')
    from test_scene_cli import _cli_experiment

    scene, report, closed, _ = _cli_experiment(tmp_path, monkeypatch,
                                             close_error=OSError('component close failed'))
    trace = tmp_path / 'incomplete.mcap'
    assert cli.main(['run', str(scene), '--trace', str(trace), '--report', str(report)]) == 1
    assert closed == [True]
    assert json.loads(report.read_text())['error'] == 'component close failed'
    raw = trace.read_bytes()
    replay = tmp_path / 'replay.json'
    assert cli.main(['replay', str(trace), '--report', str(replay)]) == 1
    assert json.loads(replay.read_text())['status'] == 'failed'
    assert trace.read_bytes() == raw


@pytest.mark.parametrize('route', ['run', 'replay'])
def test_missing_trace_extra_is_blocked_without_native_acquisition(tmp_path, route):
    scene = bus_scene(tmp_path)
    trace, report = tmp_path / 'capture.mcap', tmp_path / 'report.json'
    if route == 'replay':
        trace.write_bytes(b'not opened by a native backend')
    program = '''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] == 'mcap':
            raise ModuleNotFoundError('optional trace dependency absent', name=fullname)
        if fullname.split('.')[0] in {'qemu', 'mujoco', 'gazebo'}:
            raise AssertionError('native import: ' + fullname)
sys.meta_path.insert(0, Guard())
from sim.cli import main
raise SystemExit(main(sys.argv[2:]))
'''
    arguments = (['run', str(scene), '--trace', str(trace)] if route == 'run'
                 else ['replay', str(trace)])
    result = subprocess.run([sys.executable, '-I', '-c', program, str(ROOT),
        *arguments, '--report', str(report)], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 78, result.stderr
    value = json.loads(report.read_text())
    assert value['status'] == 'blocked' and 'trace extra' in value['error']
