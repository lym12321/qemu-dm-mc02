"""Unix stream acquisition with cleanup before retry or ownership transfer."""

from __future__ import annotations

import math
import socket
import time


def _note_cleanup_error(primary: BaseException, name: str,
                        error: BaseException) -> None:
    primary.add_note(f"cleanup {name}.close() failed: "
                     f"{type(error).__name__}: {error}")


def connect_unix(path: str, timeout: float, *, socket_module=socket,
                 clock=time, report_cleanup_error=_note_cleanup_error):
    """Return a connected non-blocking socket, now owned by the caller."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and non-negative")
    deadline = clock.monotonic() + timeout
    last_error: OSError | None = None
    while clock.monotonic() < deadline:
        sock = socket_module.socket(socket_module.AF_UNIX,
                                    socket_module.SOCK_STREAM)
        try:
            sock.connect(path)
            sock.setblocking(False)
            return sock
        except BaseException as exc:
            try:
                sock.close()
            except BaseException as error:
                report_cleanup_error(exc, "Unix connection", error)
                raise exc
            if not isinstance(exc, OSError):
                raise
            last_error = exc
            clock.sleep(0.01)
    raise TimeoutError(f"cannot connect to {path}: {last_error}")
