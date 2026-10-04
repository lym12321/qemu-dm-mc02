#!/usr/bin/env python3
"""Bounded QEMU lifecycle for external integration scripts.

Read a Python test from stdin. Socket names in braces are replaced in the
QEMU command and --python-arg values. Register/MMIO tests belong in libqtest;
this adapter only owns processes, temporary sockets and failure diagnostics.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time


def positive_seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def stop(process: subprocess.Popen[bytes] | None, *,
         process_group: bool = True) -> None:
    if process is None:
        return
    # Each process owns its group, including workers launched by the test.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if process_group:
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise RuntimeError("process remained alive after TERM/KILL deadlines")
            continue
        if sig == signal.SIGKILL or not process_group:
            break
        # Reap the direct child, then also remove any remaining descendants.


@dataclass(frozen=True)
class RunningQemu:
    """Borrowed process and private socket paths, valid inside running_qemu."""

    process: subprocess.Popen[bytes]
    start_ns: int
    sockets: dict[str, Path]
    stderr_path: Path

    def expand(self, value: str) -> str:
        for name, path in self.sockets.items():
            value = value.replace("{" + name + "}", str(path))
        return value.replace("{qemu_pid}", str(self.process.pid)).replace(
            "{qemu_start_ns}", str(self.start_ns))


def _validate_inputs(command: Sequence[str], sockets: Sequence[str],
                     wait_sockets: Sequence[str] | None,
                     startup_timeout: float
                     ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], float]:
    if (isinstance(command, (str, bytes)) or not command or
            any(not isinstance(value, str) or "\0" in value for value in command)):
        raise ValueError("a nonempty command of strings without NUL is required")
    if not command[0]:
        raise ValueError("command executable must not be empty")
    if isinstance(sockets, (str, bytes)):
        raise ValueError("socket names must be unique short identifiers")
    names = tuple(sockets)
    if (not names or any(not isinstance(name, str) or
                         not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", name)
                         for name in names) or len(set(names)) != len(names)):
        raise ValueError("socket names must be unique short identifiers")
    if isinstance(wait_sockets, (str, bytes)):
        raise ValueError("wait sockets must be declared socket names")
    wait_names = names if wait_sockets is None else tuple(wait_sockets)
    if any(name not in names for name in wait_names):
        raise ValueError("wait sockets must be declared socket names")
    if (isinstance(startup_timeout, bool) or
            not isinstance(startup_timeout, (int, float)) or
            not math.isfinite(startup_timeout) or startup_timeout <= 0):
        raise ValueError("startup timeout must be finite and positive")
    return tuple(command), names, wait_names, float(startup_timeout)


def _stderr_tail(path: Path) -> str:
    with path.open("rb") as stderr:
        stderr.seek(0, os.SEEK_END)
        stderr.seek(max(0, stderr.tell() - 12000))
        return stderr.read(12000).decode(errors="replace")


@contextmanager
def running_qemu(command: Sequence[str], *, sockets: Sequence[str],
                 wait_sockets: Sequence[str] | None = None,
                 startup_timeout: float = 3.0) -> Iterator[RunningQemu]:
    """Own one process group and private /tmp sockets with bounded startup/stop.

    Socket existence is transport readiness, never application readiness. None
    waits for every declared socket; an empty wait_sockets waits for no socket.
    Cleanup preserves the first exception, including interrupts, and attaches
    cleanup failures and at most 12000 stderr bytes as exception notes.
    Consumers own protocol connections and must check subsequent process exits.
    """
    command, names, wait_names, timeout = _validate_inputs(
        command, sockets, wait_sockets, startup_timeout)
    temporary = None
    stderr = None
    stderr_path = None
    process = None
    primary = None
    try:
        temporary = tempfile.TemporaryDirectory(
            prefix="dm-qemu.integration.", dir="/tmp")
        paths = {name: Path(temporary.name) / f"{name}.sock" for name in names}
        stderr_path = Path(temporary.name) / "qemu.stderr"
        stderr = stderr_path.open("wb")
        expanded = list(command)
        for name, path in paths.items():
            expanded = [value.replace("{" + name + "}", str(path))
                        for value in expanded]
        start_ns = time.monotonic_ns()
        process = subprocess.Popen(
            expanded, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=stderr, start_new_session=True)
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"QEMU exited during startup ({process.returncode})")
            ready = all(path.exists() and stat.S_ISSOCK(path.stat().st_mode)
                        for path in (paths[name] for name in wait_names))
            elapsed = (time.monotonic_ns() - start_ns) / 1_000_000_000
            if elapsed >= timeout:
                raise RuntimeError("QEMU socket startup deadline expired")
            if ready:
                break
            time.sleep(min(0.01, timeout - elapsed))
        yield RunningQemu(process, start_ns, paths, stderr_path)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        errors = []
        tail = ""
        if process is not None:
            try:
                stop(process)
            except BaseException as exc:
                errors.append(("process stop", exc))
        if stderr is not None:
            try:
                stderr.close()
            except BaseException as exc:
                errors.append(("stderr close", exc))
        if stderr is not None and stderr_path is not None:
            try:
                tail = _stderr_tail(stderr_path)
            except BaseException as exc:
                errors.append(("stderr read", exc))
        if temporary is not None:
            try:
                temporary.cleanup()
            except BaseException as exc:
                errors.append(("temporary directory", exc))
        if errors:
            message = "QEMU cleanup failed: " + "; ".join(
                f"{label}: {exc}" for label, exc in errors)
            if primary is None:
                first = errors[0][1]
                primary = (first if isinstance(first, (KeyboardInterrupt, SystemExit))
                           else RuntimeError(message))
                if primary is first:
                    BaseException.add_note(primary, message)
                if tail:
                    BaseException.add_note(primary, "QEMU stderr tail:\n" + tail)
                raise primary
            BaseException.add_note(primary, message)
        if primary is not None and tail:
            BaseException.add_note(primary, "QEMU stderr tail:\n" + tail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", action="append", default=[])
    parser.add_argument("--wait-socket", action="append", default=[])
    parser.add_argument("--python-arg", action="append", default=[])
    parser.add_argument("--startup-timeout", type=positive_seconds, default=3.0)
    parser.add_argument("--timeout", type=positive_seconds, default=20.0)
    parser.add_argument("--expect-qemu-quit", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        _validate_inputs(command, args.socket, args.wait_socket or None,
                         args.startup_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    source = sys.stdin.buffer.read()
    try:
        with running_qemu(command, sockets=args.socket,
                          wait_sockets=args.wait_socket or None,
                          startup_timeout=args.startup_timeout) as owner:
            test = None
            primary = None
            try:
                test = subprocess.Popen(
                    [sys.executable, "-", *[owner.expand(value)
                                           for value in args.python_arg]],
                    stdin=subprocess.PIPE, start_new_session=True,
                )
                test.communicate(input=source, timeout=args.timeout)
                if test.returncode:
                    raise RuntimeError(f"integration test exited with status {test.returncode}")
                if args.expect_qemu_quit:
                    owner.process.wait(timeout=2.0)
                    if owner.process.returncode:
                        raise RuntimeError(
                            f"QEMU quit with status {owner.process.returncode}")
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    stop(test)
                except BaseException as exc:
                    message = f"integration test cleanup failed: {exc}"
                    if primary is None:
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            BaseException.add_note(exc, message)
                            raise
                        raise RuntimeError(message) from exc
                    BaseException.add_note(primary, message)
        return 0
    except BaseException as exc:
        try:
            print(f"integration failed: {exc}", file=sys.stderr)
            for note in getattr(exc, "__notes__", ()):
                print(note, file=sys.stderr)
        except Exception:
            pass
        if isinstance(exc, (OSError, RuntimeError, subprocess.TimeoutExpired)):
            return 1
        raise


if __name__ == "__main__":
    def interrupted(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
