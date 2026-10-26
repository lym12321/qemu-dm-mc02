#!/usr/bin/env python3
"""Run virtual M3508 devices on the existing QEMU co-sim worker lifecycle."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dm_mc02_sim_worker as worker
from sim.apps.can_worker import rotor_simulation_factory


def configure_parser(parser):
    parser.description = __doc__
    parser.add_argument('--m3508-id', type=int, choices=range(1, 9), action='append',
                        help='CAN ESC ID, repeat for multiple motors (default: 1)')
    parser.add_argument('--can-delay-ns', type=int, default=0,
                        help='deterministic virtual delivery delay (default: 0)')
    parser.add_argument('--m3508-timeout-ms', type=int, default=100,
                        help='virtual command timeout (default: 100 ms)')
    parser.add_argument('--torque-per-amp', type=float, default=.02,
                        help='uncalibrated rotor torque coefficient (default: .02 Nm/A)')


def create_simulation_factory(args):
    """Validate the fixed profile before opening any host transport."""
    if not args.fdcan or args.engine != 'null' or args.backend or args.backend_registry:
        raise ValueError('--fdcan and the default engine are required')
    ids = args.m3508_id or [1]
    if len(set(ids)) != len(ids):
        raise ValueError('--m3508-id must be unique')
    return rotor_simulation_factory(ids, delay_ns=args.can_delay_ns,
        command_timeout_ns=args.m3508_timeout_ms * 1_000_000,
        torque_per_amp=args.torque_per_amp)


def main(argv=None):
    args = worker.parse_args(argv, configure_parser=configure_parser)
    try:
        factory = create_simulation_factory(args)
    except (ValueError, TypeError) as exc:
        print(f'CAN profile: {exc}', file=sys.stderr)
        return 2
    return worker.run(args, simulation_factory=factory)


if __name__ == '__main__':
    raise SystemExit(main())
