#!/usr/bin/env python3
"""Run virtual M3508 devices on the existing QEMU co-sim worker lifecycle."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dm_mc02_sim_worker as worker
from sim.devices.dji import M3508
from sim.fabric import CanBus
from sim.fabric.endpoints import QemuCanEndpoint, SocketCanEndpoint
from sim.fabric.runtime import CanRuntime


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


def create_can_factory(args):
    """Validate the fixed profile before opening any host transport."""
    if not args.fdcan:
        raise ValueError('--fdcan is required')
    ids = args.m3508_id or [1]
    if len(set(ids)) != len(ids):
        raise ValueError('--m3508-id must be unique')
    bus = CanBus(delay_ns=args.can_delay_ns)
    devices = [M3508(bus, motor_id,
                     command_timeout_ns=args.m3508_timeout_ms * 1_000_000,
                     torque_per_amp=args.torque_per_amp) for motor_id in ids]

    def factory(can, tx):
        endpoint = QemuCanEndpoint(bus, can)
        socket_endpoint = SocketCanEndpoint(bus, tx) if tx is not None else None
        return CanRuntime(bus, endpoint, devices, socket_endpoint)
    return factory


def main(argv=None):
    args = worker.parse_args(argv, configure_parser=configure_parser)
    try:
        factory = create_can_factory(args)
    except (ValueError, TypeError) as exc:
        print(f'CAN profile: {exc}', file=sys.stderr)
        return 2
    return worker.run(args, can_factory=factory)


if __name__ == '__main__':
    raise SystemExit(main())
