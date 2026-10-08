"""Small command entry for native board debugging and manifest experiments."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import signal
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _seconds(value) -> float:
    if isinstance(value, bool):
        raise argparse.ArgumentTypeError("must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be finite and positive") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def _port(value) -> int:
    if isinstance(value, bool):
        raise argparse.ArgumentTypeError("must be an integer in 1..65535")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer in 1..65535") from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be an integer in 1..65535")
    return number


def _runtime_options(parser, *, timeout: float, paused=True, debug=True) -> None:
    parser.add_argument("--qemu", metavar="PATH", help="QEMU executable (default: project build)")
    parser.add_argument("--timeout", type=_seconds, default=timeout, metavar="SECONDS",
                        help=f"active host deadline including startup; cleanup separate (default: {timeout:g})")
    parser.add_argument("--startup-timeout", type=_seconds, default=10.0, metavar="SECONDS",
                        help="startup deadline from QEMU launch (default: 10)")
    if not debug:
        return
    parser.add_argument("--qmp", metavar="PATH", help="additional native QMP Unix socket")
    parser.add_argument("--gdb-port", type=_port, metavar="PORT",
                        help="native GDB port on 127.0.0.1, in 1..65535")
    if paused:
        parser.add_argument("--paused", action="store_true",
                            help="start paused for native GDB or interactive cont (requires --gdb-port)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sim", description="Run DM-MC02 firmware and reproducible Lab experiments.")
    groups = parser.add_subparsers(dest="group", required=True)
    board = groups.add_parser("board", help="native firmware terminal, QMP and GDB")
    boards = board.add_subparsers(dest="action", required=True)
    boards.add_parser("run", help="forward all options to tools/run-qemu.sh",
                      add_help=False)
    lab = groups.add_parser("lab", help="manifest experiments and interactive public board IO")
    labs = lab.add_subparsers(dest="action", required=True)
    run = labs.add_parser("run", help="apply a Lab manifest and report its assertions",
                         description="Run one YAML Lab manifest with native QEMU observations.")
    run.add_argument("manifest", metavar="PATH", type=Path,
                     help="YAML manifest; firmware paths are relative to its directory")
    _runtime_options(run, timeout=30.0)
    run.add_argument("--report", type=Path, metavar="PATH", help="write JSON result, including failures")
    interactive = labs.add_parser("interactive", help="observe UART/CAN and enter GPIO/key/ADC commands",
                                  description="Use a pipe or TTY for commands; enter help to list commands.")
    interactive.add_argument("--elf", required=True, type=Path, metavar="PATH", help="firmware ELF")
    interactive.add_argument("--uart", action="append", choices=("uart1", "uart2", "uart3", "uart5", "uart7", "uart10"),
                             help="attach a UART; repeat for several (default: uart1)")
    interactive.add_argument("--can", action="append", choices=("can1", "can2", "can3"),
                             help="attach a CAN channel; repeat for several")
    interactive.add_argument("--adc", action="store_true", help="enable ADC1 raw16/voltage input")
    _runtime_options(interactive, timeout=300.0)
    robot = groups.add_parser("robot", help="single-board four-wheel robot manifests")
    robots = robot.add_subparsers(dest="action", required=True)
    run = robots.add_parser("run", help="run firmware, chassis physics and optional upper processes")
    run.add_argument("manifest", type=Path, metavar="PATH",
                     help="YAML robot manifest; paths are relative to its directory")
    run.add_argument("--duration", type=_seconds, default=5.0, metavar="SECONDS",
                     help="QEMU virtual duration after optional UART ready (default: 5)")
    _runtime_options(run, timeout=30.0, paused=False)
    run.add_argument("--report", type=Path, metavar="PATH", help="write JSON result, including failures")
    arena = groups.add_parser("arena", help="two firmware robots sharing one native world")
    arenas = arena.add_subparsers(dest="action", required=True)
    run = arenas.add_parser("run", help="run an admitted two-robot Arena manifest")
    run.add_argument("manifest", type=Path, metavar="PATH",
                     help="YAML Arena manifest; robot references are relative to its directory")
    run.add_argument("--duration", type=_seconds, default=5.0, metavar="SECONDS",
                     help="shared logical world duration after all requested UART ready markers (default: 5)")
    _runtime_options(run, timeout=30.0, debug=False)
    run.add_argument("--report", type=Path, metavar="PATH", help="write JSON result, including failures")
    return parser


def _emit(event, *, member=None) -> None:
    """Keep serial bytes inert and distinguish the two observation clocks."""
    from sim.fabric import CanFrame

    endpoint = event.endpoint if member is None else f"{member}.{event.endpoint}"
    if isinstance(event.data, bytes):
        print(f"UART {endpoint} poll_sample_ns={event.timestamp_ns} {event.data!r}",
              flush=True)
    elif isinstance(event.data, CanFrame):
        frame = event.data
        print(f"CAN {endpoint} native_timestamp_ns={frame.timestamp_ns} "
              f"id=0x{frame.can_id:x} flags=0x{frame.flags:x} dlc={frame.dlc} "
              f"data={frame.data.hex()}", flush=True)
    else:
        raise TypeError("unsupported Lab observation")


def _emit_arena(event) -> None:
    _emit(event.event, member=event.name)


def _write_report(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _diagnose(error: BaseException) -> None:
    print(f"error: {error}", file=sys.stderr)
    for note in getattr(error, "__notes__", ()):
        print(note, file=sys.stderr)


def _run_lab(args) -> int:
    code = 1
    try:
        # Help and the native board launcher need no Lab/YAML dependencies.
        from sim.lab.manifest import load_manifest
        from sim.lab.runtime import run_manifest

        manifest = load_manifest(args.manifest)
        result = run_manifest(
            manifest, emit=_emit, paused=args.paused, qemu=args.qemu,
            startup_timeout=args.startup_timeout, timeout=args.timeout,
            qmp_path=args.qmp, gdb_port=args.gdb_port)
        # run_manifest returns only after every connection/process cleanup.
        payload = {"status": "passed" if result.passed else "failed",
                   "result": asdict(result)}
        code = 0 if result.passed else 1
        for failure in result.failures:
            print(f"assertion failed: {failure}", file=sys.stderr)
    except (Exception, KeyboardInterrupt) as error:
        _diagnose(error)
        payload = {"status": "failed", "error": str(error),
                   "error_type": type(error).__name__,
                   "notes": list(getattr(error, "__notes__", ()))}
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
    if args.report is not None:
        try:
            _write_report(args.report, payload)
        except (Exception, KeyboardInterrupt) as error:
            _diagnose(error)
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    if "result" in payload:
        print(f"Lab {result.name!r}: {payload['status']} "
              f"({len(result.status)} assertions)", flush=True)
    return code


def _interrupted(signum, _frame) -> None:
    # Unwind the runtime's existing process/connection owners on external TERM.
    raise KeyboardInterrupt(f"interrupted by signal {signum}")


def _run_interactive(args) -> int:
    try:
        from sim.lab.interactive import run_interactive
        result = run_interactive(
            args.elf, uarts=tuple(args.uart or ("uart1",)), cans=tuple(args.can or ()),
            adc=args.adc, emit=_emit, paused=args.paused, qemu=args.qemu,
            startup_timeout=args.startup_timeout, timeout=args.timeout,
            qmp_path=args.qmp, gdb_port=args.gdb_port)
        print(f"Lab interactive closed: {result['commands']} commands, "
              f"{result['observation_batches']} observations", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        _diagnose(error)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


def _run_robot(args) -> int:
    try:
        from sim.robots.manifest import load_manifest
        from sim.robots.runtime import run_manifest

        manifest = load_manifest(args.manifest)
        result = run_manifest(
            manifest, emit=_emit, duration=args.duration, qemu=args.qemu,
            startup_timeout=args.startup_timeout, timeout=args.timeout,
            qmp_path=args.qmp, gdb_port=args.gdb_port)
        payload = {"status": "passed", "result": asdict(result)}
        code = 0
    except (Exception, KeyboardInterrupt) as error:
        _diagnose(error)
        payload = {"status": "failed", "error": str(error),
                   "error_type": type(error).__name__,
                   "notes": list(getattr(error, "__notes__", ()))}
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
    if args.report is not None:
        try:
            _write_report(args.report, payload)
        except (Exception, KeyboardInterrupt) as error:
            _diagnose(error)
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    if code == 0:
        print(f"Robot {result.name!r}: passed (application ready: {result.ready})", flush=True)
    return code


def _run_arena(args) -> int:
    try:
        from sim.arena.manifest import load_manifest

        manifest = load_manifest(args.manifest)
        from sim.arena.runtime import run_manifest

        result = run_manifest(
            manifest, emit=_emit_arena, duration=args.duration, qemu=args.qemu,
            startup_timeout=args.startup_timeout, timeout=args.timeout)
        payload = {"status": "passed", "result": asdict(result)}
        code = 0
    except (Exception, KeyboardInterrupt) as error:
        _diagnose(error)
        payload = {"status": "failed", "error": str(error),
                   "error_type": type(error).__name__,
                   "notes": list(getattr(error, "__notes__", ()))}
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
    if args.report is not None:
        try:
            _write_report(args.report, payload)
        except (Exception, KeyboardInterrupt) as error:
            _diagnose(error)
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    if code == 0:
        print(f"Arena {result.name!r}: passed (shared world {result.origin_ns}..{result.end_ns} ns, "
              f"{len(result.boards)} boards)", flush=True)
    return code


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # The existing native launcher owns all board option admission. Preserve
    # its exact argv, stdio mux, process identity and GDB/QMP semantics.
    if arguments[:2] == ["board", "run"]:
        launcher = str(PROJECT_ROOT / "tools/run-qemu.sh")
        try:
            os.execv(launcher, [launcher, *arguments[2:]])
        except OSError as error:
            _diagnose(error)
            return 1
        return 0  # Only reachable when execv is replaced by an embedding caller.
    args = _parser().parse_args(arguments)
    previous = signal.signal(signal.SIGTERM, _interrupted)
    try:
        if args.group == "robot":
            return _run_robot(args)
        if args.group == "arena":
            return _run_arena(args)
        return _run_lab(args) if args.action == "run" else _run_interactive(args)
    finally:
        signal.signal(signal.SIGTERM, previous)
