#!/usr/bin/env python3
"""Exercise a packaged firmware CLI and its native QEMU."""

import argparse
import json
import os
import platform
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time


READY = b"SIM_READY minimal-board\n"
TAIL_BYTES = 12000


def console_case(command, *, debug, cwd, env):
    command = list(command)
    if debug:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command += ["--debug", "--gdb-port", str(port)]
    result = {"command": command, "passed": False}
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail = bytearray()
    changed = threading.Event()
    lock = threading.Lock()
    read_errors = []

    def read_output():
        try:
            while chunk := process.stdout.read1(4096):
                with lock:
                    tail.extend(chunk)
                    del tail[:-TAIL_BYTES]
                changed.set()
        except OSError as error:
            read_errors.append(str(error))
        finally:
            changed.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = started + 20

    def output():
        with lock:
            return bytes(tail)

    def wait_for(marker):
        while marker not in output():
            if process.poll() is not None:
                raise RuntimeError(f"firmware CLI exited before {marker!r}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"waiting for {marker!r}")
            changed.wait(0.05)
            changed.clear()

    def send(data):
        process.stdin.write(data)
        process.stdin.flush()

    try:
        if debug:
            # TCP admission plus the native monitor proves the public --debug
            # path exposes its GDB listener and keeps the guest reset-paused.
            # This intentionally makes no claim about GDB protocol operation.
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("GDB TCP listener did not become available")
                    changed.wait(0.05)
                    changed.clear()
            result["gdb_tcp_accept"] = True
            send(b"\x01cinfo status\n")
            wait_for(b"VM status: paused")
            if READY in output():
                raise RuntimeError("guest UART ran before native monitor cont")
            result["reset_paused_without_uart"] = True
            send(b"cont\n\x01c")
        wait_for(READY)
        result["firmware_uart_ready"] = True
        result["uart_ready_elapsed_s"] = time.monotonic() - started
        send(b"\x01cquit\n")
        process.stdin.close()
        result["exit_code"] = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if result["exit_code"] != 0:
            raise RuntimeError(f"native monitor quit returned {result['exit_code']}")
        result["monitor_quit"] = True
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            reader.join(timeout=2)
            if reader.is_alive():
                raise RuntimeError("output pipe remains open after CLI exit")
            if read_errors:
                raise RuntimeError("; ".join(read_errors))
            result["cleanup"] = "passed"
        except Exception as error:
            result["cleanup"] = f"failed: {error}"
        if not process.stdin.closed:
            process.stdin.close()
        if not reader.is_alive():
            process.stdout.close()
        result["exit_code"] = process.returncode
        result["output_tail"] = output().decode("utf-8", errors="replace")
    result["passed"] = "error" not in result and result["cleanup"] == "passed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", type=Path, required=True,
                        help="firmware executable; exercises adjacent QEMU discovery")
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists() or args.report.is_symlink():
        parser.error("--report must be a new path")
    qemu, firmware = args.qemu.resolve(), args.firmware.resolve()
    if not qemu.is_file() or not firmware.is_file():
        parser.error("--qemu and --firmware must be existing files")
    sim = args.sim.resolve()
    if not sim.is_file():
        parser.error("--sim must be an existing executable")
    command = [str(sim), "firmware", str(firmware)]
    report = {"schema": "dm-mc02/package-smoke/1", "platform": platform.platform(),
              "python": sys.executable, "qemu": str(qemu), "firmware": str(firmware),
              "sim": str(sim),
              "passed": False}
    try:
        # The standalone must find its own QEMU without a checkout, Python
        # installation or executable from the caller's PATH.
        with tempfile.TemporaryDirectory(prefix="dm-mc02 package 验证-") as directory:
            empty_path = Path(directory) / "empty-path"
            empty_path.mkdir()
            env = os.environ.copy()
            removed = ["QEMU_SYSTEM_ARM", "PYTHONHOME", "PYTHONPATH"]
            for name in removed:
                env.pop(name, None)
            paths = [str(empty_path)]
            if os.name == "nt":
                system_root = Path(os.environ["SystemRoot"])
                paths += [str(system_root / "System32"), str(system_root)]
            env["PATH"] = os.pathsep.join(paths)
            report["environment"] = {"cwd": directory, "PATH": env["PATH"],
                                     "removed_variables": removed}
            probe = subprocess.run([str(qemu), "-machine", "help"], cwd=directory,
                                   env=env, capture_output=True, timeout=10)
            report["machine_help"] = {
                "exit_code": probe.returncode,
                "stdout_tail": probe.stdout[-TAIL_BYTES:].decode("utf-8", errors="replace"),
                "stderr_tail": probe.stderr[-TAIL_BYTES:].decode("utf-8", errors="replace"),
            }
            if probe.returncode or not any(line.split()[:1] == [b"dm-mc02"]
                                          for line in probe.stdout.splitlines()):
                raise RuntimeError("packaged QEMU does not expose the dm-mc02 machine")
            report["console"] = console_case(command, debug=False, cwd=directory, env=env)
            report["debug"] = console_case(command, debug=True, cwd=directory, env=env)
        report["passed"] = report["console"]["passed"] and report["debug"]["passed"]
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if not report["passed"]:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    print(f"Package smoke {'passed' if report['passed'] else 'FAILED'}: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
