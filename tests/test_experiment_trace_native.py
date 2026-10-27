"""Opt-in real CAN/GPIO/UART recording and offline replay consumer gate.

Set DM_MC02_EXPERIMENT_TRACE_QEMU to the existing canonical QEMU binary.
Set DM_MC02_EXPERIMENT_REPLAY_PYTHON to a noneditable installed Python to
also exercise replay from outside the checkout with every native import blocked.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('build_type,skip_rx', [('Debug', False), ('Release', False), ('Debug', True)])
def test_real_can_fault_recording_replays_after_elf_is_inaccessible(
        tmp_path, monkeypatch, build_type, skip_rx):
    configured = os.environ.get('DM_MC02_EXPERIMENT_TRACE_QEMU')
    if configured is None:
        pytest.skip('set DM_MC02_EXPERIMENT_TRACE_QEMU for the separate native trace gate')
    pytest.importorskip('mcap')
    from sim import cli, composition
    from smoke.simulation_smoke import Observations, build_example, verify_can_faults

    qemu = Path(configured).resolve(strict=True)
    qemu_digest = hashlib.sha256(qemu.read_bytes()).hexdigest()
    build = build_example('can-faults', build_dir=tmp_path / 'firmware',
                          build_type=build_type, can_skip_rx=skip_rx)
    firmware = Path(build['elf'])
    document = yaml.safe_load((ROOT / 'examples/can-faults/scene.yaml').read_text())
    document['nodes']['main']['firmware'] = str(firmware)
    # 0x421 is the existing guest's nonce-derived heartbeat reply. A CAN input
    # receipt alone cannot satisfy this assertion when the guest skips RX.
    document['experiment']['assertions'].append({'before': '20ms', 'can_seen': {
        'endpoint': 'main.fdcan1', 'id': 0x421}})
    scene = tmp_path / 'scene.yaml'
    scene.write_text(yaml.safe_dump(document, sort_keys=False))
    trace, report = tmp_path / 'capture.mcap', tmp_path / 'record.json'
    observations = Observations(capture_can_faults=True)
    resources, machines = [], []
    original_registry = composition.builtin_registry

    def registry(**options):
        values = original_registry(**options)
        factory = values['machine.qemu']
        create = factory.create

        def capture(spec, context):
            machine = create(spec, context)
            machines.append(machine)
            return machine

        monkeypatch.setattr(factory, 'create', capture)
        return values

    def observe(event):
        observations(event)
        if not resources and event.kind == 'snapshot':
            info = machines[0].connection_info
            pid = info['pid']
            start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            resources.append({'pid': pid, 'starttime': start, 'sockets': dict(info['sockets'])})

    monkeypatch.setattr(composition, 'builtin_registry', registry)
    monkeypatch.setattr(cli, '_emit', observe)
    code = cli.main(['run', str(scene), '--qemu', str(qemu), '--trace', str(trace),
                     '--report', str(report)])
    assert code == (1 if skip_rx else 0)
    recorded = json.loads(report.read_text())
    result = recorded['result']
    assert result['status'] == ['pass'] * 5 + (['fail'] if skip_rx else ['pass'])
    assert result['delivered_inputs'] == 47 and result['origin_ns'] > 0
    assert result['ready_receipt']['kind'] == 'uart'
    if skip_rx:
        # Apply the unchanged independent verifier to the original five actual
        # assertions. Its nonce check must still reject apparent UART success.
        original = SimpleNamespace(**result, passed=all(item == 'pass' for item in result['status'][:5]))
        with pytest.raises(AssertionError, match='nonce-derived CAN RX replies are missing'):
            verify_can_faults(observations, original)
        independent = {'expected_failure': 'actual nonce-derived CAN RX replies are missing'}
    else:
        independent = verify_can_faults(observations, SimpleNamespace(**result, passed=True))
        assert len(independent['computed_replies']) == 36
    assert resources
    for resource in resources:
        stat = Path(f"/proc/{resource['pid']}/stat")
        assert not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[19] != resource['starttime']
        assert not any(Path(path).exists() for path in resource['sockets'].values())
    assert hashlib.sha256(qemu.read_bytes()).hexdigest() == qemu_digest
    assert hashlib.sha256(firmware.read_bytes()).hexdigest() == build['sha256']
    raw_digest = hashlib.sha256(trace.read_bytes()).hexdigest()
    hidden = firmware.with_suffix('.offline')
    firmware.rename(hidden)
    scene.unlink()
    monkeypatch.setattr(composition, 'builtin_registry', lambda **_: pytest.fail('offline registry'))
    replay_report = tmp_path / 'replay.json'
    assert cli.main(['replay', str(trace), '--report', str(replay_report)]) == code
    replayed = json.loads(replay_report.read_text())
    assert replayed['result'] == result and replayed['provenance']['mode'] == 'offline-replay'
    installed = os.environ.get('DM_MC02_EXPERIMENT_REPLAY_PYTHON')
    installed_record = {'status': 'SKIP', 'reason': 'installed replay Python not configured'}
    if installed is not None:
        installed_report = tmp_path / 'installed-replay.json'
        program = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'sim.composition' or fullname.startswith('sim.machines') or fullname.split('.')[0] in {'qemu','mujoco','gazebo','tools'}:
            raise AssertionError('offline replay imported native registry: '+fullname)
sys.meta_path.insert(0, Guard())
from sim.cli import main
raise SystemExit(main(sys.argv[1:]))
'''
        command = [installed, '-I', '-c', program, 'replay', str(trace), '--report', str(installed_report)]
        process = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=20)
        installed_record = {'argv': command, 'exit_code': process.returncode,
                            'stdout': process.stdout, 'stderr': process.stderr}
        assert process.returncode == code, process.stderr
        assert json.loads(installed_report.read_text())['result'] == result
    assert hashlib.sha256(trace.read_bytes()).hexdigest() == raw_digest
    assert hashlib.sha256(hidden.read_bytes()).hexdigest() == build['sha256']
    (tmp_path / 'native-evidence.json').write_text(json.dumps({
        'build': build, 'retained_elf': str(hidden), 'qemu_sha256': qemu_digest,
        'trace_sha256': raw_digest, 'record_exit': code, 'record': recorded,
        'replay': replayed, 'installed_replay': installed_record,
        'independent_firmware_acceptance': independent,
        'observations': observations.report(), 'resources': resources,
        'cleanup_completed': True}, indent=2) + '\n')
