#!/usr/bin/env python3
"""Bounded QEMU lifecycle for external integration scripts.

Read a Python test from stdin. Socket names in braces are replaced in the
QEMU command and --python-arg values. Register/MMIO tests belong in libqtest;
this adapter only owns processes, temporary sockets and failure diagnostics.
"""

from __future__ import annotations

import argparse
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


def stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    # Each process owns its group, including workers launched by the test.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGKILL:
            break
        # Reap the direct child, then also remove any remaining descendants.


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
    if not command:
        parser.error("a QEMU command after -- is required")
    if not args.socket:
        parser.error("at least one startup socket is required")
    if (len(set(args.socket)) != len(args.socket) or
            any(not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", name)
                for name in args.socket)):
        parser.error("socket names must be unique short identifiers")
    if any(name not in args.socket for name in args.wait_socket):
        parser.error("wait sockets must be declared with --socket")
    source = sys.stdin.buffer.read()
    qemu = None
    test = None
    with tempfile.TemporaryDirectory(prefix="dm-qemu.integration.", dir="/tmp") as tmp:
        sockets = {name: Path(tmp) / f"{name}.sock" for name in args.socket}

        def expand(value: str) -> str:
            for name, path in sockets.items():
                value = value.replace("{" + name + "}", str(path))
            return value

        stderr_path = Path(tmp) / "qemu.stderr"
        try:
            with stderr_path.open("wb") as stderr:
                qemu = subprocess.Popen(
                    [expand(value) for value in command],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=stderr, start_new_session=True,
                )
                deadline = time.monotonic() + args.startup_timeout
                while True:
                    if qemu.poll() is not None:
                        raise RuntimeError(f"QEMU exited during startup ({qemu.returncode})")
                    if all(sockets[name].exists() and
                           stat.S_ISSOCK(sockets[name].stat().st_mode)
                           for name in (args.wait_socket or args.socket)):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("QEMU socket startup deadline expired")
                    time.sleep(0.01)
                test = subprocess.Popen(
                    [sys.executable, "-", *[
                        expand(value).replace("{qemu_pid}", str(qemu.pid))
                        for value in args.python_arg
                    ]],
                    stdin=subprocess.PIPE, start_new_session=True,
                )
                test.communicate(input=source, timeout=args.timeout)
                if test.returncode:
                    raise RuntimeError(f"integration test exited with status {test.returncode}")
                if args.expect_qemu_quit:
                    qemu.wait(timeout=2.0)
                    if qemu.returncode:
                        raise RuntimeError(f"QEMU quit with status {qemu.returncode}")
            return 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"integration failed: {exc}", file=sys.stderr)
            if stderr_path.exists():
                with stderr_path.open("rb") as stderr:
                    stderr.seek(0, os.SEEK_END)
                    stderr.seek(max(0, stderr.tell() - 12000))
                    print(stderr.read().decode(errors="replace"), file=sys.stderr)
            return 1
        finally:
            stop(test)
            stop(qemu)


if __name__ == "__main__":
    def interrupted(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
