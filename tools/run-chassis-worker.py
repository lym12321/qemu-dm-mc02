#!/usr/bin/env python3
"""Run four M3508 wheels and native body IMU on one shared MuJoCo plant."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dm_mc02_sim_worker as worker
from sim.robots.four_wheel import four_wheel_simulation_factory, four_wheel_imu


def configure_parser(parser):
    parser.description = __doc__
    parser.add_argument('--chassis-model', type=Path, default=(
        Path(__file__).resolve().parents[1] / 'models/four_wheel.xml'))
    parser.add_argument('--can-delay-ns', type=int, default=0)
    parser.add_argument('--m3508-timeout-ms', type=int, default=100)
    parser.add_argument('--torque-per-amp', type=float, default=.02,
                        help='uncalibrated rotor torque coefficient in Nm/A')


def main(argv=None):
    args = worker.parse_args(argv, configure_parser=configure_parser)
    try:
        if (not args.fdcan or args.engine != 'null' or args.backend
                or args.backend_registry):
            raise ValueError('--fdcan and the default engine are required')
        factory = four_wheel_simulation_factory(
            args.chassis_model, delay_ns=args.can_delay_ns,
            command_timeout_ns=args.m3508_timeout_ms * 1_000_000,
            torque_per_amp=args.torque_per_amp)
    except ImportError as exc:
        if exc.name != 'mujoco':
            raise
        print('chassis profile: MuJoCo is required; use uv run --frozen --extra mujoco',
              file=sys.stderr)
        return 2
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f'chassis profile: {exc}', file=sys.stderr)
        for note in getattr(exc, '__notes__', ()):
            print(f'chassis profile: {note}', file=sys.stderr)
        return 2
    try:
        return worker.run(args, simulation_factory=factory, imu_reader=four_wheel_imu)
    except (BufferError, ConnectionError, OSError, TimeoutError, TypeError,
            ValueError, RuntimeError, worker.SocketCANError) as exc:
        print(f'chassis worker: {exc}', file=sys.stderr)
        for note in getattr(exc, '__notes__', ()):
            print(f'chassis worker: {note}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
