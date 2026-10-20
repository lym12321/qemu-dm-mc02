"""Lazy synchronous adapter around the one pinned QEMU QMP implementation.

Greeting, framing, command IDs and event routing remain owned by QEMU. An
installed sim package needs an explicit backend checkout for native execution;
validation and backend descriptions never import that optional source tree.
"""
from __future__ import annotations

import atexit
import importlib
from pathlib import Path
import sys

from .errors import BackendUnavailable

_SESSION_CLASSES = {}


def backend_root(value=None) -> Path:
    if value is not None:
        candidate = Path(value).resolve()
        if not (candidate / 'qemu.lock').is_file():
            raise BackendUnavailable(f'QEMU backend root needs qemu.lock: {candidate}')
        return candidate
    for candidate in Path(__file__).resolve().parents:
        if (candidate / 'qemu.lock').is_file():
            return candidate
    raise BackendUnavailable('native QEMU execution needs --backend-root pointing to an initialized checkout')


def qmp_session_class(root=None):
    """Resolve the canonical checkout and return its bounded session adapter."""
    root = backend_root(root)
    python_root = root / 'qemu/upstream/python'
    if not (python_root / 'qemu/qmp/legacy.py').is_file():
        raise BackendUnavailable(f'initialize the canonical QEMU submodule under {root}')
    cached = _SESSION_CLASSES.get(python_root)
    if cached is not None:
        return cached
    sys.path.insert(0, str(python_root))
    legacy = importlib.import_module('qemu.qmp.legacy')
    if not Path(legacy.__file__).resolve().is_relative_to(python_root):
        raise BackendUnavailable('QMP must come from the selected qemu/upstream/python')

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

    _SESSION_CLASSES[python_root] = QmpSession
    return QmpSession
