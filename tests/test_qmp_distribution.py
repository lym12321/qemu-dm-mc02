"""Installed-client identity and bundle admission; no QEMU process is required."""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from sim.api import BackendUnavailable
from sim.machines.qemu import qmp

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('qemu_bundle', ROOT / 'tools/dm_mc02_qemu_bundle.py')
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def test_missing_installed_client_never_loads_checkout_or_changes_import_path(monkeypatch, tmp_path):
    (tmp_path / 'qemu/upstream/python/qemu/qmp').mkdir(parents=True)
    (tmp_path / 'qemu/upstream/python/qemu/qmp/legacy.py').write_text('raise AssertionError("source fallback")')
    before = list(sys.path)

    def missing(_name):
        raise importlib.metadata.PackageNotFoundError('qemu')

    monkeypatch.setattr(qmp.importlib.metadata, 'distribution', missing)
    monkeypatch.setattr(qmp.importlib, 'import_module', lambda *_: pytest.fail('client import before admission'))
    with pytest.raises(BackendUnavailable, match='install the pinned QEMU Python wheel'):
        qmp.qmp_client_provenance()
    assert sys.path == before


def test_matching_package_name_or_version_cannot_substitute_other_source(monkeypatch, tmp_path):
    path = tmp_path / 'qemu/qmp/__init__.py'
    path.parent.mkdir(parents=True)
    path.write_text('# unrelated package with matching version\n')
    distribution = SimpleNamespace(version=qmp.CLIENT_VERSION, files=[Path('qemu/qmp/__init__.py')],
                                   locate_file=lambda name: tmp_path / name)
    monkeypatch.setattr(qmp.importlib.metadata, 'distribution', lambda _name: distribution)
    monkeypatch.setattr(qmp.importlib, 'import_module', lambda *_: pytest.fail('unverified client executed'))
    with pytest.raises(BackendUnavailable, match='source fingerprint'):
        qmp.qmp_client_provenance()
    distribution.version = '0.0.0'
    with pytest.raises(BackendUnavailable, match='version differs'):
        qmp.qmp_client_provenance()


def test_legacy_tool_import_defers_the_optional_client(monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError('qemu')

    monkeypatch.setattr(qmp.importlib.metadata, 'distribution', missing)
    monkeypatch.setattr(qmp, '_SESSION_CLASS', None)
    wrapper = importlib.import_module('tools.dm_mc02_qmp')
    with pytest.raises(BackendUnavailable, match='QMP client is unavailable'):
        wrapper.QmpSession('/unused/qmp.sock')


def test_bundle_rejects_unpinned_source_and_existing_output_before_build(tmp_path):
    source, output = tmp_path / 'source', tmp_path / 'bundle'
    source.mkdir()
    (source / 'qemu.lock').write_text('fork_commit=' + '0' * 40 + '\n')
    with pytest.raises(ValueError, match='qemu.lock differs'):
        bundle.build(source, output, python=sys.executable)
    assert not output.exists()
    output.mkdir()
    with pytest.raises(ValueError, match='new directory'):
        bundle.build(source, output, python=sys.executable)
    with pytest.raises(ValueError, match='supplied together'):
        bundle.build(source, tmp_path / 'another', python=sys.executable, binary=Path(sys.executable))


def test_bundle_binary_requires_exact_bytes_and_source_identity(tmp_path):
    binary = tmp_path / 'qemu-system-arm'
    binary.write_bytes(b'actual selected executable')
    provenance = tmp_path / 'binary.json'
    identity = {'schema': 'dm-mc02/qemu-binary/1', 'source_commit': qmp.CLIENT_COMMIT,
                'sha256': bundle.file_hash(binary)}
    provenance.write_text(json.dumps(identity))
    assert bundle.binary_provenance(provenance, binary) == identity
    binary.write_bytes(b'changed executable')
    with pytest.raises(ValueError, match='selected binary'):
        bundle.binary_provenance(provenance, binary)


def test_wheel_verifier_rejects_missing_or_duplicate_modules(tmp_path):
    path = tmp_path / 'qemu.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('qemu/qmp/legacy.py', b'# incomplete wheel')
    with pytest.raises(ValueError, match='source fingerprint'):
        bundle.verify_wheel(path)
    with zipfile.ZipFile(path, 'a') as archive, pytest.warns(UserWarning, match='Duplicate'):
        archive.writestr('qemu/qmp/legacy.py', b'# duplicate')
    with pytest.raises(ValueError, match='duplicate'):
        bundle.verify_wheel(path)


def test_bundle_verifier_rejects_partial_completion_and_unlisted_files(tmp_path):
    manifest = {'schema': 'dm-mc02/qemu-bundle/1', 'status': 'building', 'files': {}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='incomplete'):
        bundle.verify(tmp_path)
    manifest['status'] = 'passed'
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'unlisted').write_bytes(b'incomplete inventory')
    with pytest.raises(ValueError, match='inventory differs'):
        bundle.verify(tmp_path)


def test_bundle_command_timeout_preserves_failure_and_reaps_child(tmp_path):
    commands = []
    with pytest.raises(subprocess.TimeoutExpired):
        bundle.run([sys.executable, '-c', 'import time; time.sleep(10)'],
                   cwd=tmp_path, commands=commands, timeout=.05)
    assert len(commands) == 1
    assert commands[0]['interrupted_or_timed_out']
    assert commands[0]['reaped'] and commands[0]['identity_gone']
