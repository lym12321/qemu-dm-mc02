"""Synchronous tooling adapter for the canonical QEMU QMP client.

QEMU owns greeting negotiation, JSON framing, command IDs and event routing.
No installed-package or sibling-checkout fallback is permitted.
"""
from pathlib import Path
import atexit
import sys

_qemu_python = Path(__file__).resolve().parents[1] / "qemu/upstream/python"
sys.path.insert(0, str(_qemu_python))

from qemu.qmp.legacy import QEMUMonitorProtocol  # noqa: E402
import qemu.qmp.legacy as _legacy  # noqa: E402

if not Path(_legacy.__file__).resolve().is_relative_to(_qemu_python):
    raise ImportError("QMP must come from qemu/upstream/python")


class QmpSession(QEMUMonitorProtocol):
    """Connect and negotiate once; retain the scripts' two result conventions."""

    def __init__(
        self,
        address,
        timeout=2.0,
        connect_timeout=None,
        close_timeout=2.0,
    ):
        self._initial_connect_timeout = None
        self._disconnect_timeout = None
        self._close_timeout = close_timeout
        self._atexit_registered = False
        super().__init__(address)
        self.settimeout(timeout)
        self._initial_connect_timeout = (
            timeout if connect_timeout is None else connect_timeout
        )
        try:
            self.connect()
        finally:
            self._initial_connect_timeout = None
        atexit.register(self.close)
        self._atexit_registered = True

    def _sync(self, future, timeout=None):
        # legacy.connect() has no timeout argument. Apply one to its existing
        # upstream coroutine without taking ownership of QMP negotiation.
        if timeout is None and self._initial_connect_timeout is not None:
            timeout = self._initial_connect_timeout
        if timeout is None and self._disconnect_timeout is not None:
            timeout = self._disconnect_timeout
        return super()._sync(future, timeout)

    def command(self, name, arguments=None):
        """Return the command value; QEMU raises on a QMP error response."""
        return self.cmd(name, **(arguments or {}))

    def command_raw(self, name, arguments=None):
        """Return QEMU's response envelope for explicit negative assertions."""
        return self.cmd_raw(name, arguments)

    def close(self):
        # QEMU normally closes the peer after replying to quit. Command
        # errors still propagate; EOF while releasing the transport is normal.
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
