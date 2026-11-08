"""QEMU process admission and command construction, independent of board ports."""

import math
import os
from pathlib import Path
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def positive_seconds(value) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value <= 0):
        raise ValueError("timeout must be finite and positive")
    return float(value)


def regular_file(path, label: str, *, executable=False) -> Path:
    path = Path(path).resolve()
    if not path.is_file() or not os.access(path, os.X_OK if executable else os.R_OK):
        raise ValueError(f"{label} is not {'executable' if executable else 'readable'}: {path}")
    return path


def resolve_qemu(value) -> Path:
    if getattr(sys, 'frozen', False):
        default = Path(sys._MEIPASS) / 'bin' / ('qemu-system-arm.exe' if sys.platform == 'win32' else 'qemu-system-arm')
    else:
        default = PROJECT_ROOT / 'build/qemu/qemu-system-arm'
    value = value or os.environ.get("QEMU_SYSTEM_ARM") or default
    if isinstance(value, str) and "/" not in value:
        value = shutil.which(value) or value
    return regular_file(value, "QEMU", executable=True)


def qmp_socket_path(path) -> Path | None:
    if path is None:
        return None
    if not isinstance(path, (str, Path)) or "\0" in str(path):
        raise ValueError("QMP socket must be a path without NUL")
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise ValueError("QMP socket needs an existing parent, a new path, and at most 100 bytes")
    path = path.parent.resolve() / path.name
    if len(os.fsencode(path)) > 100 or not path.parent.is_dir():
        raise ValueError("QMP socket needs an existing parent, a new path, and at most 100 bytes")
    return path


def native_qemu_command(qemu, sockets, *, machine_arguments, serial_slots,
                        qmp_path=None, gdb_port=None):
    """Compose native process/debug channels from an admitted machine profile."""
    command = [str(qemu), *machine_arguments,
               "-nodefaults", "-display", "none", "-monitor", "none", "-S",
               "-qmp", "unix:{qmp},server=on,wait=off"]
    for port in serial_slots:
        if port in sockets:
            command += ["-chardev", f"socket,id={port},path={{{port}}},server=on,wait=off",
                        "-serial", f"chardev:{port}"]
        else:
            command += ["-serial", "none"]
    if qmp_path is not None:
        command += ["-qmp", f"unix:{str(qmp_path).replace(',', ',,')},server=on,wait=off"]
    if gdb_port is not None:
        command += ["-gdb", f"tcp:127.0.0.1:{gdb_port}"]
    return command


def terminal_qemu_command(qemu, *, machine_arguments, serial_slots, uart,
                          sockets, qmp_path=None, gdb_port=None):
    """Compose a native console launch; QEMU owns execution and terminal IO."""
    command = [str(qemu), *machine_arguments,
               "-nodefaults", "-display", "none",
               "-monitor", "stdio" if uart is None else "none"]
    for port in serial_slots:
        if port == uart:
            command += ["-serial", "mon:stdio"]
        elif port in sockets:
            path = str(sockets[port]).replace(',', ',,')
            command += ["-chardev", f"socket,id={port},path={path},server=on,wait=off",
                        "-serial", f"chardev:{port}"]
        else:
            command += ["-serial", "none"]
    if qmp_path is not None:
        path = str(qmp_path).replace(',', ',,')
        command += ["-qmp", f"unix:{path},server=on,wait=off"]
    if gdb_port is not None:
        command += ["-S", "-gdb", f"tcp:127.0.0.1:{gdb_port}"]
    return command
