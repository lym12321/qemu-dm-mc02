"""Installed entry point for the single Simulation execution path."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math
from pathlib import Path
import signal
import sys


def _seconds(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('must be finite and positive') from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return result


def _port(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('port must be in 1..65535') from None
    if not 1 <= result <= 65535:
        raise argparse.ArgumentTypeError('port must be in 1..65535')
    return result


def _parser():
    parser = argparse.ArgumentParser(prog='sim', description='Compose and run embedded simulations.')
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate', help='validate sim/v1 without starting native resources')
    validate.add_argument('scene', type=Path)
    run = commands.add_parser('run', help='run a sim/v1 scene through Simulation')
    run.add_argument('scene', type=Path)
    run.add_argument('--qemu', help='native QEMU executable (or QEMU_SYSTEM_ARM)')
    run.add_argument('--backend-root', type=Path, help='optional checkout for the default native executable')
    run.add_argument('--timeout', type=_seconds, help='total host budget including startup')
    run.add_argument('--startup-timeout', type=_seconds, default=10.0)
    run.add_argument('--gdb-port', type=_port, help='single machine native GDB port')
    run.add_argument('--qmp', type=Path, help='single machine additional native QMP socket')
    run.add_argument('--report', type=Path, help='create a new JSON report after cleanup')
    return parser


def _json(value):
    if isinstance(value, bytes):
        return {'hex': value.hex()}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _emit(event):
    # Keep firmware control bytes inert; preserve explicit source clocks.
    if event.kind != 'snapshot':
        print(json.dumps(_json(event), ensure_ascii=True), flush=True)


def _interactive(simulation, scene):
    """One bounded command pump and at most one common step per iteration."""
    import os
    import select
    import time
    from sim.config.yaml import parse_time

    print('Commands: run, pause, step [1ms], state, inject ENDPOINT JSON, reset, quit', flush=True)
    running = False
    pending = bytearray()
    discarding = False
    stdin_open = True
    next_tick = time.monotonic()
    while True:
        wait = max(0.0, min(.05, next_tick - time.monotonic())) if running else .05
        # Drain already-read lines before reading again, keeping memory bounded
        # even when a pipe supplies many short commands faster than execution.
        readable = []
        if b'\n' not in pending and stdin_open:
            readable, _, _ = select.select([sys.stdin], [], [], wait)
        elif not pending and not stdin_open:
            return
        if readable:
            raw = os.read(sys.stdin.fileno(), 4096)
            if not raw:
                stdin_open = False
                if pending and not discarding:
                    pending.extend(b'\n')
            if discarding:
                _, newline, raw = raw.partition(b'\n')
                if not newline:
                    raw = b''
                else:
                    discarding = False
            pending.extend(raw)
        if b'\n' in pending:
            raw_line, _, rest = pending.partition(b'\n')
            pending[:] = rest
            try:
                if len(raw_line) > 4096:
                    raise ValueError('command exceeds 4096 bytes')
                line = raw_line.decode('utf-8').strip()
                command, _, arguments = line.partition(' ')
                value = None
                if command == 'step':
                    value = parse_time(arguments or '1ms', 'step', zero=False)
                elif command == 'inject':
                    endpoint, separator, encoded = arguments.partition(' ')
                    if not separator:
                        raise ValueError('inject requires ENDPOINT JSON')
                    payload = json.loads(encoded)
                    # Invalid payloads are recoverable; exhaustion of the
                    # execution queue is terminal even before input delivery.
                    simulation.schedule_input(endpoint, payload, at_ns=simulation.t_sim_ns)
                elif arguments or command not in ('', 'quit', 'run', 'pause', 'state', 'reset'):
                    raise ValueError('unknown command or arguments')
            except (ValueError, TypeError, KeyError, BufferError) as error:
                if simulation.state == 'failed':
                    raise
                print(f'error: {error}', file=sys.stderr, flush=True)
            else:
                # Execution errors must escape to the common cleanup/report
                # path, including ValueError raised after a component effect.
                if command == 'quit':
                    return
                if command == 'run':
                    running = True
                    next_tick = time.monotonic()
                elif command == 'pause':
                    running = False
                    simulation.pause()
                elif command == 'step':
                    running = False
                    simulation.step(value)
                elif command == 'state':
                    print(json.dumps({'state': _json(simulation.state),
                                      'time_ns': simulation.t_sim_ns}), flush=True)
                elif command == 'reset':
                    running = False
                    simulation.reset()
        if len(pending) > 4096 and b'\n' not in pending:
            pending.clear()
            discarding = True
            print('error: command exceeds 4096 bytes; discarded through newline', file=sys.stderr, flush=True)
        if running and time.monotonic() >= next_tick:
            if simulation.t_sim_ns >= scene.duration_ns:
                running = False
                simulation.pause()
            else:
                simulation.step(min(scene.quantum_ns, scene.duration_ns - simulation.t_sim_ns))
                next_tick = time.monotonic() + scene.quantum_ns / 1e9
        simulation.check_deadline()


def _interrupted(_signum, _frame):
    raise KeyboardInterrupt('simulation interrupted')


def main(argv=None):
    args = _parser().parse_args(argv)
    simulation = None
    error = None
    code = 0
    result = None
    previous = signal.signal(signal.SIGTERM, _interrupted)
    try:
        from sim.composition import builtin_registry
        from sim.config.scene import load_scene
        from sim.runtime import Simulation

        registry = builtin_registry(**({'qemu': args.qemu, 'backend_root': args.backend_root,
                                       'gdb_port': args.gdb_port, 'qmp_path': args.qmp}
                                      if args.command == 'run' else {}))
        scene = load_scene(args.scene, registry)
        if args.command == 'validate':
            print(f'valid: {args.scene} ({len(scene.topology.nodes)} nodes)')
            return 0
        if (args.gdb_port is not None or args.qmp is not None) and sum(
                node.kind == 'machine.qemu' for node in scene.topology.nodes) != 1:
            raise ValueError('--gdb-port/--qmp require exactly one QEMU machine')
        timeout = args.timeout or (300.0 if scene.mode == 'interactive' else 30.0)
        simulation = Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns,
                                mode=scene.mode, host_timeout_s=timeout,
                                startup_timeout_s=args.startup_timeout, on_event=_emit)
        simulation.start()
        if scene.mode == 'interactive':
            _interactive(simulation, scene)
        else:
            simulation.run_until(scene.duration_ns)
        result = {'time_ns': simulation.t_sim_ns}
    except BaseException as caught:
        error = caught
        code = 130 if isinstance(caught, KeyboardInterrupt) else 1
        from sim.api import BackendUnavailable
        if isinstance(caught, BackendUnavailable):
            code = 78
    finally:
        if simulation is not None:
            try:
                simulation.close()
            except BaseException as cleanup:
                if error is None:
                    error = cleanup
                    code = 130 if isinstance(cleanup, KeyboardInterrupt) else 1
                else:
                    error.add_note(f'Simulation cleanup: {type(cleanup).__name__}: {cleanup}')
        signal.signal(signal.SIGTERM, previous)
    payload = ({'status': 'passed', 'result': result} if error is None else
               {'status': 'blocked' if code == 78 else 'failed', 'error': str(error),
                'error_type': type(error).__name__, 'notes': getattr(error, '__notes__', [])})
    if args.command == 'run' and args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open('x', encoding='utf-8') as output:
                output.write(json.dumps(payload, ensure_ascii=True, indent=2) + '\n')
        except BaseException as report_error:
            if error is None:
                error = report_error
                code = 130 if isinstance(report_error, KeyboardInterrupt) else 1
            else:
                error.add_note(f'report: {type(report_error).__name__}: {report_error}')
    if error is not None:
        print(f'error: {error}', file=sys.stderr)
        for note in getattr(error, '__notes__', ()):
            print(note, file=sys.stderr)
    else:
        print(f"passed: time_ns={result['time_ns']}")
    return code
