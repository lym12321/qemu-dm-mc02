"""Linux raw SocketCAN acquisition; record codecs belong to consumers."""

from __future__ import annotations

import errno
import platform
import socket


class SocketCANError(RuntimeError):
    """Raised when the host cannot provide the requested SocketCAN feature."""


def require_socketcan(*, socket_module=socket, platform_module=platform) -> None:
    if platform_module.system() != "Linux":
        raise SocketCANError("SocketCAN is supported only on Linux")
    if not hasattr(socket_module, "AF_CAN"):
        raise SocketCANError("this Python build has no AF_CAN support")
    if not hasattr(socket_module, "CAN_RAW"):
        raise SocketCANError("this Python build has no CAN_RAW support")


def open_socketcan(interface: str, *, enable_fd: bool = True,
                   socket_module=socket, platform_module=platform):
    """Open and bind a raw socket, transferring ownership only on success."""
    require_socketcan(socket_module=socket_module,
                      platform_module=platform_module)
    sock = None
    try:
        sock = socket_module.socket(socket_module.AF_CAN,
                                    socket_module.SOCK_RAW,
                                    socket_module.CAN_RAW)
        if enable_fd:
            option = getattr(socket_module, "CAN_RAW_FD_FRAMES", 5)
            sock.setsockopt(socket_module.SOL_CAN_RAW, option, 1)
        sock.bind((interface,))
        return sock
    except BaseException as exc:
        primary = exc
        if isinstance(exc, OSError):
            if exc.errno in (errno.ENODEV, errno.ENXIO):
                primary = SocketCANError(
                    f"SocketCAN interface is unavailable: {interface}")
            else:
                primary = SocketCANError(
                    f"cannot open SocketCAN interface {interface}: {exc}")
        if sock is not None:
            try:
                sock.close()
            except BaseException as error:
                primary.add_note(f"cleanup SocketCAN.close() failed: "
                                 f"{type(error).__name__}: {error}")
        if primary is exc:
            raise
        raise primary from exc
