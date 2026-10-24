#!/usr/bin/env python3
"""Build the locked full QEMU Python wheel and an optional native bundle.

Only committed python/ source is built, in a temporary directory. The selected
interpreter must already contain setuptools and wheel; uv runs offline without
build isolation. A binary bundle additionally requires explicit build provenance.
The bundle records host runtime requirements; it is not a static executable.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LICENSE_HASHES = {
    'COPYING': '6f04ae8364d0079a192b14635f4b1da294ce18724c034c39a6a41d1b09df6100',
    'COPYING.LIB': 'dc626520dcd53a22f727af3ee42c770e56c97a64fe3adb063799d8ab032fe551',
}
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))
from sim.machines.qemu.qmp import (
    CLIENT_COMMIT, CLIENT_SOURCE_FILES, CLIENT_SOURCE_SHA256, CLIENT_VERSION,
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(entries):
    names = sorted(name for name in entries if name.startswith('qemu/')
                   and (name.endswith('.py') or name.endswith('/py.typed')))
    records = [name + '\0' + sha256(entries[name]) + '\n' for name in names]
    return len(names), sha256(''.join(records).encode())


def verify_wheel(path):
    """Require the complete locked package, including the grouped-command patch."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError('wheel contains duplicate members')
        if any(PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
               for name in names):
            raise ValueError('unsafe wheel member')
        entries = {name: archive.read(name) for name in names}
    if source_fingerprint(entries) != (CLIENT_SOURCE_FILES, CLIENT_SOURCE_SHA256):
        raise ValueError('wheel source fingerprint differs from the locked QEMU client')
    metadata = entries.get(f'qemu-{CLIENT_VERSION}.dist-info/METADATA', b'').decode()
    if f'Name: qemu\n' not in metadata or f'Version: {CLIENT_VERSION}\n' not in metadata:
        raise ValueError('wheel distribution identity differs from the locked client')
    for name, expected in LICENSE_HASHES.items():
        copies = [data for path, data in entries.items()
                  if path.startswith(f'qemu-{CLIENT_VERSION}.dist-info/')
                  and PurePosixPath(path).name == name]
        if len(copies) != 1 or sha256(copies[0]) != expected:
            raise ValueError(f'wheel requires the original {name} license text')
    return {'version': CLIENT_VERSION, 'source_commit': CLIENT_COMMIT,
            'source_sha256': CLIENT_SOURCE_SHA256, 'source_files': CLIENT_SOURCE_FILES,
            'sha256': file_hash(path), 'bytes': Path(path).stat().st_size}


def run(argv, *, cwd, commands, env=None, timeout=60):
    """Own and reap each finite build/probe process, preserving failure output."""
    argv = list(map(str, argv))
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=False, start_new_session=True)
    record = {'argv': argv, 'cwd': str(cwd), 'pid': process.pid, 'timeout_s': timeout}
    commands.append(record)
    stat = Path(f'/proc/{process.pid}/stat')
    starttime = None
    try:
        try:
            starttime = stat.read_text().rsplit(')', 1)[1].split()[19]
        except FileNotFoundError:
            pass
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException as primary:
        try:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=2)
        except BaseException as cleanup:
            primary.add_note(f'command cleanup failed: {cleanup!r}')
            record['cleanup_error'] = repr(cleanup)
        record['interrupted_or_timed_out'] = True
        raise
    finally:
        record.update(exit=process.returncode, reaped=process.poll() is not None,
                      starttime=starttime, identity_gone=not stat.exists(),
                      elapsed_s=time.monotonic() - started)
        if 'stdout' in locals():
            record.update(stdout_bytes=len(stdout), stdout_sha256=sha256(stdout),
                          stdout_tail='<binary output>' if b'\0' in stdout else stdout[-12000:].decode(errors='replace'),
                          stderr_tail=stderr[-12000:].decode(errors='replace'))
    if process.returncode:
        raise RuntimeError(f'command exited {process.returncode}: {argv!r}; {stderr.decode(errors="replace")[-2000:]}')
    return stdout


def binary_provenance(path, binary):
    value = json.loads(Path(path).read_text())
    if (type(value) is not dict or value.get('schema') != 'dm-mc02/qemu-binary/1'
            or value.get('source_commit') != CLIENT_COMMIT
            or value.get('sha256') != file_hash(binary)):
        raise ValueError('binary provenance must identify the selected binary and locked source commit')
    return value


def build(source, output, *, python, uv='uv', binary=None, provenance=None):
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists():
        raise ValueError('bundle output must be a new directory')
    if (binary is None) != (provenance is None):
        raise ValueError('--qemu and --binary-provenance must be supplied together')
    lock_path = source / 'qemu.lock'
    lock = dict(line.split('=', 1) for line in lock_path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)
    if lock.get('fork_commit') != CLIENT_COMMIT:
        raise ValueError('qemu.lock differs from the client source fingerprint')
    binary = Path(binary).resolve() if binary is not None else None
    identity = binary_provenance(provenance, binary) if binary is not None else None
    if binary is not None and not os.access(binary, os.X_OK):
        raise ValueError('QEMU binary must be executable')
    output.mkdir(parents=True)
    manifest = {'schema': 'dm-mc02/qemu-bundle/1', 'status': 'building',
                'qemu_lock': lock, 'commands': [], 'files': {},
                'runtime_note': 'Native binary requires compatible Linux host libraries; libraries are not bundled.'}
    commands = manifest['commands']
    child = source / 'qemu/upstream'
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    try:
        head = run(['git', '-C', child, 'rev-parse', 'HEAD'], cwd=source, commands=commands).decode().strip()
        if head != CLIENT_COMMIT:
            raise ValueError('QEMU checkout HEAD differs from qemu.lock')
        run(['git', '-C', child, 'diff', '--exit-code', CLIENT_COMMIT, '--', 'python'],
            cwd=source, commands=commands)
        snapshot = run(['git', '-C', child, 'archive', '--format=tar', CLIENT_COMMIT,
                        'python', 'COPYING', 'COPYING.LIB'], cwd=source, commands=commands)
        (output / 'qemu-python-source.tar').write_bytes(snapshot)
        epoch = run(['git', '-C', child, 'show', '-s', '--format=%ct', CLIENT_COMMIT],
                    cwd=source, commands=commands).decode().strip()
        environment['SOURCE_DATE_EPOCH'] = epoch
        with tempfile.TemporaryDirectory(prefix='dm-qemu.wheel.', dir='/tmp') as temporary:
            temporary = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
                archive.extractall(temporary, filter='data')
            # Supply the original texts to setuptools' standard license discovery.
            # Package source and per-file license notices remain byte-for-byte.
            for name in ('COPYING', 'COPYING.LIB'):
                shutil.copyfile(temporary / name, temporary / 'python' / name)
            (output / 'python').mkdir()
            run([uv, 'build', '--wheel', '--offline', '--no-build-isolation',
                 '--python', Path(python).absolute(), '--out-dir', output / 'python',
                 temporary / 'python'], cwd=temporary, env=environment, commands=commands)
            wheels = list((output / 'python').glob('*.whl'))
            if len(wheels) != 1:
                raise ValueError('the QEMU package build must produce exactly one wheel')
            manifest['client'] = verify_wheel(wheels[0])
            (output / 'licenses').mkdir()
            for name in ('COPYING', 'COPYING.LIB'):
                shutil.copyfile(temporary / name, output / 'licenses' / name)
        shutil.copyfile(lock_path, output / 'qemu.lock')
        if binary is not None:
            (output / 'bin').mkdir()
            destination = output / 'bin/qemu-system-arm'
            shutil.copy2(binary, destination)
            if file_hash(destination) != identity['sha256']:
                raise ValueError('binary changed while copying')
            shutil.copyfile(provenance, output / 'binary-provenance.json')
            manifest['binary'] = {'path': 'bin/qemu-system-arm', 'provenance': identity,
                'version': run([destination, '--version'], cwd=output, env=environment,
                               commands=commands).decode().splitlines()[0],
                'source_identity_note': 'Build provenance supplied by the caller; packaging does not revalidate the compiler or native build.'}
        manifest['files'] = {str(path.relative_to(output)): {
            'sha256': file_hash(path), 'bytes': path.stat().st_size}
            for path in sorted(output.rglob('*')) if path.is_file()}
        manifest['status'] = 'passed'
    except BaseException as error:
        manifest.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def verify(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('schema') != 'dm-mc02/qemu-bundle/1' or manifest.get('status') != 'passed':
        raise ValueError('bundle is incomplete')
    actual = {str(path.relative_to(directory)) for path in directory.rglob('*')
              if path.is_file() and path != directory / 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('bundle file inventory differs')
    for name, expected in manifest['files'].items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('unsafe bundle path')
        local = directory / name
        if local.is_symlink() or file_hash(local) != expected['sha256'] or local.stat().st_size != expected['bytes']:
            raise ValueError(f'bundle content differs: {name}')
    wheels = list((directory / 'python').glob('*.whl'))
    if len(wheels) != 1 or verify_wheel(wheels[0]) != manifest['client']:
        raise ValueError('bundle client differs')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    create = subparsers.add_parser('build')
    create.add_argument('--source', type=Path, default=ROOT)
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--python', type=Path, default=Path(sys.executable))
    create.add_argument('--uv', default='uv')
    create.add_argument('--qemu', type=Path)
    create.add_argument('--binary-provenance', type=Path,
        help='JSON: schema dm-mc02/qemu-binary/1, source_commit and binary sha256; optional build evidence')
    check = subparsers.add_parser('verify')
    check.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    try:
        result = (verify(args.directory) if args.command == 'verify' else
                  build(args.source, args.output, python=args.python, uv=args.uv,
                        binary=args.qemu, provenance=args.binary_provenance))
    except (Exception, KeyboardInterrupt) as error:
        print(f'error: {error}', file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(json.dumps({'status': result['status'], 'client': result['client']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
