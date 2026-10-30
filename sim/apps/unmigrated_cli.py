"""Unmigrated application CLI, retained only for its existing regression gates.

This module is not the public Simulation entry point.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
        prog="sim", description="Native firmware terminal and interactive Lab tools.")
    groups = parser.add_subparsers(dest="group", required=True)
    board = groups.add_parser("board", help="native firmware terminal, QMP and GDB")
    boards = board.add_subparsers(dest="action", required=True)
    boards.add_parser("run", help="forward all options to tools/run-qemu.sh",
                      add_help=False)
    lab = groups.add_parser("lab", help="native interactive public board IO")
    labs = lab.add_subparsers(dest="action", required=True)
    interactive = labs.add_parser("interactive", help="observe UART/CAN and enter GPIO/key/ADC commands",
                                  description="Use a pipe or TTY for commands; enter help to list commands.")
    interactive.add_argument("--elf", required=True, type=Path, metavar="PATH", help="firmware ELF")
    interactive.add_argument("--uart", action="append", choices=("uart1", "uart2", "uart3", "uart5", "uart7", "uart10"),
                             help="attach a UART; repeat for several (default: uart1)")
    interactive.add_argument("--can", action="append", choices=("can1", "can2", "can3"),
                             help="attach a CAN channel; repeat for several")
    interactive.add_argument("--adc", action="store_true", help="enable ADC1 raw16/voltage input")
    _runtime_options(interactive, timeout=300.0)
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


def _diagnose(error: BaseException) -> None:
    print(f"error: {error}", file=sys.stderr)
    for note in getattr(error, "__notes__", ()):
        print(note, file=sys.stderr)


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
        return _run_interactive(args)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
