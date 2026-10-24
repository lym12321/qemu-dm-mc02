"""DM-MC02 profile data; importing it has no process or QMP dependency."""
from dataclasses import replace
from pathlib import Path

from sim.api.topology import immutable as _freeze
from sim.boards import (QemuBoard, digital_level, drain_paused_can,
                        observe_initial_paused_can, wait_paused_can_input)
from sim.fabric.digital import DigitalSample
from sim.fabric.frame import CanFrame
from sim.transports import StreamTransport
from sim.transports.can_stream import CanStream
from sim.transports.cosim import StepFramedSocket

from .errors import BackendUnavailable
from .imu import QemuImuEndpoint

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


class DmMc02Profile:
    """Fixed board admission and wire adapters, independent of process ownership."""

    serial_slots = ('cosim', *UARTS, 'can1', 'can2', 'can3')

    @staticmethod
    def machine_arguments(firmware):
        return ('-machine', 'dm-mc02,fdcan-host-ack=on', '-kernel', str(firmware))

    describe = staticmethod(describe)
    validate_input = staticmethod(validate_input)

    @staticmethod
    def validate_connections(node, topology, descriptions):
        inputs = {}
        for link in topology.links:
            for endpoint in (link.source, link.target):
                if endpoint.node != node.name:
                    continue
                port = descriptions[node.name].port(endpoint.port)
                if port.protocol not in ('can', 'digital', 'imu', 'uart'):
                    raise ValueError(f'unsupported QEMU connection: {endpoint} ({port.protocol})')
                if port.protocol != 'digital' and 'external_input' not in port.capabilities:
                    continue
                identity = input_identity(endpoint.port)
                if identity in inputs:
                    raise ValueError(f'QEMU pin aliases {inputs[identity]} and {endpoint} '
                                     f'share physical pin {identity}')
                inputs[identity] = endpoint

    @staticmethod
    def create_adapter(spec, context, backend):
        return DmMc02Adapter(spec, context, backend)


class _CanInput:
    def __init__(self, machine, port):
        self.machine, self.port = machine, port

    def receive(self, frame):
        self.machine._receive_can(self.port, frame)


class _UartInput:
    def __init__(self, machine, port):
        self.machine, self.port = machine, port

    def receive_uart(self, data):
        self.machine.input(self.port, data)


class _DigitalOutput:
    def __init__(self, machine, port):
        self.machine, self.port = machine, port

    def read_digital(self):
        return self.machine._read_digital(self.port)


class _DigitalInput:
    def __init__(self, machine, port):
        self.machine, self.port = machine, port

    def receive_digital(self, level):
        self.machine._receive_digital(self.port, level)


class _ImuInput:
    def __init__(self, machine):
        self.machine = machine

    def receive_imu(self, sample):
        self.machine._receive_imu(sample)



class DmMc02Adapter:
    """Borrow native backend access and own only this board's wire projections."""

    def __init__(self, spec, ctx, backend):
        self._firmware, self._uarts = admit_options(spec)
        self._remote = spec.options.get('remote', False)
        self._ctx, self._backend = ctx, backend
        self._can_ports = tuple(port for port in CAN_PORTS if port in ctx.connected_ports)
        self._can = {port: ctx.attach(port, _CanInput(self, port)) for port in self._can_ports}
        self._uart = {port: ctx.attach(port, _UartInput(backend, port))
                      for port in self._uarts if port in ctx.connected_ports}
        self._imu_connected = 'imu' in ctx.connected_ports
        self._imu = None
        if self._imu_connected:
            ctx.attach('imu', _ImuInput(self))
        digital_ports = tuple(port for port in ctx.connected_ports
                              if port == 'key' or port.startswith('gpio.'))
        self._digital_outputs = tuple(port for port in digital_ports if port.endswith('.out'))
        self._digital_samples = {}
        for port in digital_ports:
            endpoint = _DigitalOutput(self, port) if port.endswith('.out') else _DigitalInput(self, port)
            ctx.attach(port, endpoint)
        self._board = self._snapshot = None
        self._streams = {}
        self._can_state = {}
        self._closed = False
        self.stream_types = {}
        if self._imu_connected:
            self.stream_types['cosim'] = StepFramedSocket
        self.stream_types.update({port: StreamTransport for port in self._uarts})
        if self._remote:
            self.stream_types['uart5'] = StreamTransport
        self.stream_types.update({port.replace('fdcan', 'can'): CanStream for port in self._can_ports})

    def launch_command(self, qemu, sockets, *, qmp_path, gdb_port):
        from sim.boards.launch import qemu_command
        return qemu_command(self._firmware, qemu, sockets,
                            qmp_path=qmp_path, gdb_port=gdb_port)

    def start(self):
        self._streams = self._backend.streams
        self._board = QemuBoard(self._backend)
        properties = self._backend.command('qom-list', {'path': '/machine'})
        required = {'board-state'} | ({'can-execution-state'} if self._can_ports else set())
        if (not isinstance(properties, list) or not required <= {
                item.get('name') for item in properties if isinstance(item, dict)}):
            raise BackendUnavailable('QEMU lacks required native board/CAN observation properties')
        snapshot = self._board.snapshot()
        origin = self._backend.native_origin_ns
        if snapshot['timestamp_ns'] != origin:
            raise RuntimeError('native snapshot and execution origin disagree')
        self._complete_snapshot(snapshot, self._backend.t_sim_ns)
        if self._imu_connected:
            self._imu = QemuImuEndpoint(self._streams['cosim'], context=self._ctx,
                                        check=self._backend.check)
            self._imu.start(origin)
        for port in self._can_ports:
            wire = port.replace('fdcan', 'can')
            self._can_state[port] = observe_initial_paused_can(
                self._backend, self._streams[wire], timestamp_ns=origin,
                controller=f'dm-mc02.{port}', chardev=wire,
                deadline=self._backend.deadline_monotonic, check=self._backend.check)
        self._ctx.emit('snapshot', 'snapshot', self._snapshot,
                       source_time_ns=origin, mapped_time_ns=self._backend.t_sim_ns,
                       delivery_time_ns=self._backend.t_sim_ns, quality='exact')

    def service(self, cut_ns):
        if self._imu is not None:
            self._imu.service(cut_ns)

    def complete_cut(self, cut_ns, target_ns):
        uart_batches = self._collect(cut_ns)
        snapshot = self._board.snapshot()
        if snapshot['timestamp_ns'] != target_ns:
            raise RuntimeError('native snapshot moved outside the completed target')
        self._complete_snapshot(snapshot, cut_ns)
        for port, payload in uart_batches:
            if port in self._uart:
                self._uart[port].send(payload, source_time_ns=target_ns,
                                      mapped_time_ns=cut_ns, quality='upper_bound')
            self._ctx.emit(port, 'uart', payload, source_time_ns=target_ns,
                           mapped_time_ns=cut_ns, delivery_time_ns=cut_ns, quality='upper_bound')
        self._ctx.emit('snapshot', 'snapshot', self._snapshot,
                       source_time_ns=target_ns, mapped_time_ns=cut_ns,
                       delivery_time_ns=cut_ns, quality='exact')

    def _complete_snapshot(self, snapshot, mapped_time_ns):
        # Project through the public native helper before freezing its mapping.
        samples = {}
        for port in self._digital_outputs:
            endpoint = port.removesuffix('.out')
            pin = endpoint[5:]
            driven = snapshot['gpio'][pin[0]]['output_mask'] & (1 << int(pin[1:]))
            level = digital_level(endpoint, snapshot) if driven else None
            samples[port] = DigitalSample(level, snapshot['timestamp_ns'], mapped_time_ns)
        frozen = _freeze(snapshot)
        self._snapshot, self._digital_samples = frozen, samples

    def _read_digital(self, port):
        """Read a completed source projection without QMP, CPU work or IO."""
        self._ctx.check_io()
        if self._closed or self._backend.pending is not None or port not in self._digital_samples:
            raise RuntimeError(f'digital source has no completed sample: {self._ctx.node_id}.{port}')
        return self._digital_samples[port]

    def _receive_digital(self, port, level):
        """Deliver a physical line level; external key inputs use pressedness."""
        if type(level) is not int or level not in (0, 1):
            raise ValueError(f'digital input requires integer 0 or 1: {self._ctx.node_id}.{port}')
        self._ctx.check_io()
        try:
            self._backend.input(port, level == 0 if port == 'key' else level)
        except BaseException as error:
            error.add_note(f'digital input failed: {self._ctx.node_id}.{port}')
            raise

    def _collect(self, cut_ns):
        target = self._backend.to_native(cut_ns)
        for port in self._can_ports:
            wire = port.replace('fdcan', 'can')
            def publish(raw, port=port):
                frame = CanFrame(raw[0], raw[1], raw[2], raw[4], raw[3])
                mapped = self._backend.to_sim(frame.timestamp_ns)
                if mapped > cut_ns:
                    raise RuntimeError('QEMU emitted a CAN frame beyond its completed target')
                # Context owns the earliest delivery cut, never raw wire time.
                self._can[port].send(replace(frame, timestamp_ns=mapped))
                self._ctx.emit(port, 'can-tx', frame,
                               source_time_ns=frame.timestamp_ns, mapped_time_ns=mapped,
                               delivery_time_ns=cut_ns, quality='exact')
            drain_paused_can(self._backend, self._streams[wire], publish,
                             target_ns=target, controller=f'dm-mc02.{port}', chardev=wire,
                             tx_dropped=self._can_state[port]['tx']['tx_dropped'],
                             deadline=self._backend.deadline_monotonic, check=self._backend.check)
        uart_batches = []
        for port in self._uarts:
            stream = self._streams[port]
            self._backend.check()
            stream.flush()
            stream.read_once()
            self._backend.check()
            if stream.buffer:
                payload = bytes(stream.buffer)
                stream.buffer.clear()
                uart_batches.append((port, payload))
        return uart_batches

    def _receive_can(self, port, frame):
        self._backend.check()
        if self._backend.pending is not None or not self._backend.started:
            raise RuntimeError('CAN input requires a completed paused QEMU cut')
        if not isinstance(frame, CanFrame):
            raise TypeError('QEMU CAN input requires a CanFrame')
        stamp = self._backend.to_native(self._ctx.t_sim_ns)
        wire = port.replace('fdcan', 'can')
        state = self._can_state[port]
        goal = state['rx']['completed_frames'] + 1
        if goal >= 0xffffffffffffffff:
            raise OverflowError('QEMU CAN input receipt counter exhausted')
        stream = self._streams[wire]
        stream.send(frame.can_id, frame.flags, frame.dlc, stamp, frame.data)
        receipt = wait_paused_can_input(
            self._backend, stream, target_ns=stamp,
            controller=f'dm-mc02.{port}', chardev=wire,
            completed_frames=goal, deadline=self._backend.deadline_monotonic, check=self._backend.check,
            initial=self._backend.t_sim_ns == self._ctx.session.t_sim_ns)
        state['rx'] = receipt
        self._ctx.emit(port, 'can-input-processed', _freeze(receipt),
                       source_time_ns=stamp, mapped_time_ns=self._ctx.t_sim_ns,
                       delivery_time_ns=self._ctx.t_sim_ns, quality='exact')

    def _receive_imu(self, sample):
        self._backend.check()
        self._ctx.check_io()
        if self._backend.pending is not None or self._imu is None:
            raise RuntimeError('IMU input requires a completed paused QEMU cut')
        self._backend.validate_cut()
        # Native time admission precedes any protocol or transport effect.
        self._backend.to_native(self._ctx.t_sim_ns)
        self._imu.receive_imu(sample)

    def input(self, port, payload):
        if port == 'key':
            self._board.set_key(payload)
        elif port.startswith('gpio.'):
            self._board.set_gpio(port[5:], payload)
        elif port in self._uarts:
            if not isinstance(payload, bytes):
                raise TypeError('UART input must be bytes')
            if len(payload) > 4096:
                raise ValueError('one UART input is limited to 4096 bytes')
            if payload:
                self._streams[port].send(payload)
        elif port == 'remote' and self._remote:
            if type(payload) is not bytes:
                raise TypeError('remote input must be raw bytes')
            if len(payload) > 4096:
                raise ValueError('one remote input is limited to 4096 bytes')
            if payload:
                self._streams['uart5'].send(payload)
        else:
            raise ValueError(f'QEMU input port is not supported or opened: {port}')

    def observe(self):
        """Return the last completed immutable native snapshot, without IO."""
        if self._snapshot is None:
            raise RuntimeError('QEMU machine has no completed snapshot')
        return self._snapshot

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._imu is not None:
            self._imu.close()
