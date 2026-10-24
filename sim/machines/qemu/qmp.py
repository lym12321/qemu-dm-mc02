"""Lazy adapter around the installed, pinned full QEMU Python distribution.

Greeting, framing, IDs, events and grouped commands remain owned by QEMU. The
source fingerprint covers that distribution's Python modules and typing markers,
including the fork's bounded cmd_many extension. No source-tree fallback exists.
"""
from __future__ import annotations

import atexit
import hashlib
import importlib
import importlib.machinery
import importlib.metadata
from pathlib import Path
import sys

from .errors import BackendUnavailable

CLIENT_VERSION = '0.6.1.0a1'
CLIENT_COMMIT = '263bfd8adf272be63177dafc6b5f1dd41e0a1389'
CLIENT_SOURCE_SHA256 = 'b13bf026b77c907b7ef162f802c8395ec1c5edfd0d3c7d9ca114655bb15e4133'
CLIENT_SOURCE_FILES = 24
_SESSION_CLASS = None


def backend_root(value=None) -> Path | None:
    """Optional checkout discovery for default binary paths, never client code."""
    if value is not None:
        candidate = Path(value).resolve()
        if not (candidate / 'qemu.lock').is_file():
            raise BackendUnavailable(f'QEMU backend root needs qemu.lock: {candidate}')
        return candidate
    for candidate in Path(__file__).resolve().parents:
        if (candidate / 'qemu.lock').is_file():
            return candidate
    return None


def qmp_client_provenance() -> dict:
    """Verify installed source bytes and import selection before executing QMP."""
    diagnostic = ('install the pinned QEMU Python wheel produced by '
                  'tools/dm_mc02_qemu_bundle.py; an arbitrary qemu package is unsupported')
    try:
        distribution = importlib.metadata.distribution('qemu')
    except importlib.metadata.PackageNotFoundError as exc:
        raise BackendUnavailable(f'QMP client is unavailable; {diagnostic}') from exc
    if distribution.version != CLIENT_VERSION:
        raise BackendUnavailable(f'QMP client version differs from {CLIENT_VERSION}; {diagnostic}')
    entries = sorted(str(path) for path in distribution.files or ()
                     if str(path).startswith('qemu/')
                     and (str(path).endswith('.py') or str(path).endswith('/py.typed')))
    try:
        records = [name + '\0' + hashlib.sha256(
            Path(distribution.locate_file(name)).read_bytes()).hexdigest() + '\n'
                   for name in entries]
    except OSError as exc:
        raise BackendUnavailable(f'QMP client source is incomplete; {diagnostic}') from exc
    fingerprint = hashlib.sha256(''.join(records).encode()).hexdigest()
    if len(entries) != CLIENT_SOURCE_FILES or fingerprint != CLIENT_SOURCE_SHA256:
        raise BackendUnavailable(f'QMP client source fingerprint differs from {CLIENT_COMMIT}; {diagnostic}')
    expected = Path(distribution.locate_file('qemu/qmp/__init__.py')).resolve()
    # PathFinder checks selection without importing a potentially shadowing module.
    namespace = importlib.machinery.PathFinder.find_spec('qemu', sys.path)
    selected = (importlib.machinery.PathFinder.find_spec(
        'qemu.qmp', namespace.submodule_search_locations)
        if namespace is not None and namespace.submodule_search_locations else None)
    if (namespace is None or namespace.origin is not None or selected is None or selected.origin is None
            or Path(selected.origin).resolve() != expected):
        raise BackendUnavailable('QMP import is shadowed; the installed pinned distribution must own qemu.qmp')
    package_root = expected.parent.parent
    for name, module in tuple(sys.modules.items()):
        if name == 'qemu' or name.startswith('qemu.'):
            filename = getattr(module, '__file__', None)
            if filename and not Path(filename).resolve().is_relative_to(package_root):
                raise BackendUnavailable('QMP import is already owned by another source')
    return {'distribution': 'qemu', 'version': distribution.version,
            'source_commit': CLIENT_COMMIT, 'source_sha256': fingerprint,
            'source_files': len(entries), 'package_root': str(package_root)}


def qmp_session_class():
    """Return the bounded adapter of the installed verified client, lazily."""
    global _SESSION_CLASS
    if _SESSION_CLASS is not None:
        return _SESSION_CLASS
    qmp_client_provenance()
    legacy = importlib.import_module('qemu.qmp.legacy')
    if not callable(getattr(legacy.QEMUMonitorProtocol, 'cmd_many', None)):
        raise BackendUnavailable('the pinned QMP client requires bounded cmd_many')

    class QmpSession(legacy.QEMUMonitorProtocol):
        """Connect/negotiate once, retaining the existing scripts' conventions."""

        def __init__(self, address, timeout=2.0, connect_timeout=None,
                     close_timeout=2.0):
            self._initial_connect_timeout = None
            self._disconnect_timeout = None
            self._close_timeout = close_timeout
            self._atexit_registered = False
            super().__init__(address)
            self.settimeout(timeout)
            self._initial_connect_timeout = timeout if connect_timeout is None else connect_timeout
            try:
                self.connect()
            finally:
                self._initial_connect_timeout = None
            atexit.register(self.close)
            self._atexit_registered = True

        def _sync(self, future, timeout=None):
            if timeout is None and self._initial_connect_timeout is not None:
                timeout = self._initial_connect_timeout
            if timeout is None and self._disconnect_timeout is not None:
                timeout = self._disconnect_timeout
            return super()._sync(future, timeout)

        def command(self, name, arguments=None):
            return self.cmd(name, **(arguments or {}))

        def command_many(self, commands):
            return self.cmd_many(commands)

        def command_raw(self, name, arguments=None):
            return self.cmd_raw(name, arguments)

        def close(self):
            self._disconnect_timeout = self._close_timeout
            try:
                super().close()
            except EOFError:
                pass
            finally:
                self._disconnect_timeout = None
                if self._atexit_registered:
                    atexit.unregister(self.close)
                    self._atexit_registered = False

    _SESSION_CLASS = QmpSession
    return QmpSession
