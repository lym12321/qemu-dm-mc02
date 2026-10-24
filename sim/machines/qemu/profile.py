"""DM-MC02 profile data; importing it has no process or QMP dependency."""
from pathlib import Path

from sim.api import NodeDescription, PortSpec
from sim.boards.launch import UARTS, regular_file
from sim.gpio import validate_gpio_pin

CAN_PORTS = ('fdcan1', 'fdcan2', 'fdcan3')
GENERIC_UARTS = tuple(port for port in UARTS if port != 'uart5')
EXTERNAL_INPUT = frozenset({'external_input'})


def admit_options(spec):
    options = spec.options
    unknown = set(options) - {'model', 'firmware', 'uarts', 'remote'}
    if unknown:
        raise ValueError(f'unknown QEMU machine options: {sorted(unknown)}')
    if options.get('model', 'dm-mc02') != 'dm-mc02':
        raise ValueError('unsupported QEMU model; expected dm-mc02')
    if type(options.get('remote', False)) is not bool:
        raise ValueError('QEMU remote must be a boolean')
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
    if (any(not isinstance(port, str) or port not in GENERIC_UARTS for port in uarts)
            or len(set(uarts)) != len(uarts)):
        raise ValueError('UART endpoints must be distinct uart1/2/3/7/10; UART5 belongs to remote')
    return path, uarts


def describe(spec):
    _, uarts = admit_options(spec)
    ports = [PortSpec(port, 'uart', capabilities=EXTERNAL_INPUT) for port in uarts]
    if spec.options.get('remote', False):
        ports += [PortSpec('remote', 'remote', direction='in', capabilities=EXTERNAL_INPUT)]
    ports += [PortSpec(port, 'can') for port in CAN_PORTS]
    ports += [PortSpec('imu', 'imu', direction='in', category='signal')]
    ports += [PortSpec('key', 'digital', direction='in', category='signal',
                       capabilities=EXTERNAL_INPUT),
              PortSpec('snapshot', 'snapshot', direction='out', category='signal')]
    ports += [PortSpec(f'gpio.{bank}{pin}', 'digital', direction='in', category='signal',
                       capabilities=EXTERNAL_INPUT)
              for bank in 'ABCDEFGH' for pin in range(16)]
    ports += [PortSpec(f'gpio.{bank}{pin}.out', 'digital', direction='out', category='signal')
              for bank in 'ABCDEFGH' for pin in range(16)]
    return NodeDescription(ports=tuple(ports), capabilities=frozenset({'machine', 'input'}))


def input_identity(port):
    """All input/output spellings reserve their one physical native pin."""
    return 'gpio.A15' if port == 'key' else port.removesuffix('.out')


def validate_input(spec, port, payload, *, connected_ports=()):
    """Validate payloads using a spec already admitted by describe(), without IO."""
    uarts = tuple(spec.options.get('uarts', ('uart1',)))
    if port == 'key':
        if type(payload) is not bool:
            raise TypeError('key input must be a boolean')
        value = payload
    elif isinstance(port, str) and port.startswith('gpio.'):
        validate_gpio_pin(port[5:])
        if type(payload) is not int:
            raise TypeError('GPIO input must be an integer')
        if payload not in (0, 1):
            raise ValueError('GPIO input must be 0 or 1')
        value = payload
    elif port == 'remote' and spec.options.get('remote', False):
        if type(payload) is not bytes:
            raise TypeError('remote input must be raw bytes')
        if len(payload) > 4096:
            raise ValueError('one remote input is limited to 4096 bytes')
        value = payload
    elif port in uarts:
        if isinstance(payload, str):
            if len(payload) > 4096:
                raise ValueError('one UART input is limited to 4096 bytes')
            value = str.encode(payload, 'utf-8')
        elif isinstance(payload, bytes):
            value = bytes(payload)
        else:
            raise TypeError('UART input must be bytes or a UTF-8 string')
        if len(value) > 4096:
            raise ValueError('one UART input is limited to 4096 bytes')
    else:
        raise ValueError(f'QEMU port cannot accept external inputs: {port}')
    identity = input_identity(port)
    if any(input_identity(connected) == identity for connected in connected_ports):
        raise ValueError(f'QEMU input is owned by a connection: {port}')
    return value
