"""DM-MC02 profile data; importing it has no process or QMP dependency."""
from pathlib import Path

from sim.api import NodeDescription, PortSpec
from sim.boards.launch import UARTS, regular_file

CAN_PORTS = ('fdcan1', 'fdcan2', 'fdcan3')


def admit_options(spec):
    options = spec.options
    unknown = set(options) - {'model', 'firmware', 'uarts'}
    if unknown:
        raise ValueError(f'unknown QEMU machine options: {sorted(unknown)}')
    if options.get('model', 'dm-mc02') != 'dm-mc02':
        raise ValueError('unsupported QEMU model; expected dm-mc02')
    firmware = options.get('firmware')
    if not isinstance(firmware, (str, Path)) or not str(firmware) or '\0' in str(firmware):
        raise ValueError('QEMU machine firmware must be a nonempty path without NUL')
    path = Path(firmware)
    if not path.is_absolute():
        path = Path(spec.base_dir) / path
    path = regular_file(path, 'firmware')
    selected = options.get('uarts', ('uart1',))
    if not isinstance(selected, (list, tuple)):
        raise ValueError('QEMU machine uarts must be a list')
    uarts = tuple(selected)
    if (any(not isinstance(port, str) or port not in UARTS for port in uarts)
            or len(set(uarts)) != len(uarts)):
        raise ValueError('UART endpoints must be distinct uart1/2/3/5/7/10')
    return path, uarts


def describe(spec):
    _, uarts = admit_options(spec)
    ports = [PortSpec(port, 'uart') for port in uarts]
    ports += [PortSpec(port, 'can') for port in CAN_PORTS]
    ports += [PortSpec('key', 'digital', direction='in', category='signal'),
              PortSpec('snapshot', 'snapshot', direction='out', category='signal')]
    ports += [PortSpec(f'gpio.{bank}{pin}', 'digital', category='signal')
              for bank in 'ABCDEFGH' for pin in range(16)]
    return NodeDescription(ports=tuple(ports), capabilities=frozenset({'machine', 'input'}))
