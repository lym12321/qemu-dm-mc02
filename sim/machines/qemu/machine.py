"""Firmware machine capability over native QEMU execution and borrowed wires.

This adapter owns connections; running_qemu remains the sole process/socket/tmp
owner. Native CPU targets, processed CAN records, FIFO acceptance and firmware
consumption are separate observations. UART timestamps are read upper bounds.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import select
import sys
import time

from sim.api import StepResult
from sim.api.topology import immutable as _freeze
from sim.boards import (QemuBoard, digital_level, drain_paused_can, observe_initial_paused_can,
                        wait_paused_can_input)
from sim.boards.launch import qemu_command, qmp_socket_path, resolve_qemu
from sim.fabric.frame import CanFrame
from sim.fabric.digital import DigitalSample
from sim.transports import StreamTransport, connect_unix
from sim.transports.can_stream import CanStream
from sim.transports.cosim import StepFramedSocket

from .deadline import deadline_qmp_call
from .errors import BackendUnavailable
from .imu import QemuImuEndpoint
from .process import running_qemu
from .profile import CAN_PORTS, admit_options, describe, input_identity, validate_input
from .qmp import backend_root as resolve_backend_root, qmp_session_class

_SIGNED64_MAX = 0x7fffffffffffffff


def _native_time(value, name):
    if type(value) is not int or not 0 <= value <= _SIGNED64_MAX:
        raise ValueError(f'{name} must be signed64 nonnegative nanoseconds')
    return value


class _QmpBoundary:
    def __init__(self, owner):
        self.owner = owner

    def command(self, name, arguments=None):
        return self.owner._call(lambda: self.owner._qmp.command(name, arguments))

    def command_many(self, commands):
        return self.owner._call(lambda: self.owner._qmp.command_many(commands))


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


class QemuFactory:
    """Explicit backend factory; static descriptions do not load native QMP."""

    def __init__(self, *, qemu=None, backend_root=None, gdb_port=None,
                 qmp_path=None):
        self.qemu = qemu
        self.backend_root = backend_root
        if gdb_port is not None and (type(gdb_port) is not int or not 1 <= gdb_port <= 65535):
            raise ValueError('GDB port must be an integer in 1..65535')
        self.gdb_port = gdb_port
        self.qmp_path = qmp_socket_path(qmp_path)

    def describe(self, spec):
        return describe(spec)

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        return validate_input(spec, port, payload, connected_ports=connected_ports)

    def validate_topology(self, topology, descriptions, registry):
        owners = {'gdb_port': {}, 'qmp_path': {}}
        inputs = {}
        qemu_nodes = set()
        for node in topology.nodes:
            factory = registry[node.kind]
            if not isinstance(factory, QemuFactory):
                continue
            qemu_nodes.add(node.name)
            for name, seen in owners.items():
                value = getattr(factory, name)
                if value is None:
                    continue
                if value in seen:
                    raise ValueError(f'QEMU nodes {seen[value]} and {node.name} share {name}: {value}')
                seen[value] = node.name
        for link in topology.links:
            for endpoint in (link.source, link.target):
                if endpoint.node not in qemu_nodes:
                    continue
                port = descriptions[endpoint.node].port(endpoint.port)
                if port.protocol not in ('can', 'digital', 'imu', 'uart'):
                    raise ValueError(f'unsupported QEMU connection: {endpoint} ({port.protocol})')
                if port.protocol != 'digital' and 'external_input' not in port.capabilities:
                    continue
                identity = (endpoint.node, input_identity(endpoint.port))
                if identity in inputs:
                    raise ValueError(f'QEMU pin aliases {inputs[identity]} and {endpoint} '
                                     f'share physical pin {identity[1]}')
                inputs[identity] = endpoint

    def create(self, spec, ctx):
        return QemuMachine(spec, ctx, self)


class QemuMachine:
    """One owned machine with split target request and completion operations."""

    def __init__(self, spec, ctx, factory):
        self._firmware, self._uarts = admit_options(spec)
        self._remote = spec.options.get('remote', False)
        self._ctx, self._factory = ctx, factory
        self._can_ports = tuple(port for port in CAN_PORTS if port in ctx.connected_ports)
        # Connections attach before any process creation. Runtime owns the buses.
        self._can = {port: ctx.attach(port, _CanInput(self, port))
                     for port in self._can_ports}
        self._uart = {port: ctx.attach(port, _UartInput(self, port))
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
        self._manager = self._owner = self._qmp = self._board = None
        self._peers = []
        self._streams = {}
        self._can_state = {}
        self._origin_ns = None
        self._last_time_ns = ctx.session.t_sim_ns
        self._pending = None
        self._snapshot = None
        self._closed = False
        self._boundary = _QmpBoundary(self)

    def _check(self):
        if self._closed:
            raise RuntimeError('QEMU machine is closed')
        self._ctx.check_deadline()
        if self._owner is not None and self._owner.process.poll() is not None:
            raise RuntimeError(f'QEMU exited ({self._owner.process.returncode})')

    @property
    def _deadline(self):
        return self._ctx.deadline_monotonic

    def _call(self, callback):
        self._check()
        result = deadline_qmp_call(self._qmp, self._deadline, 'QEMU machine', callback)
        self._check()
        return result

    def _query(self, name, arguments=None):
        return self._boundary.command(name, arguments)

    def _remaining(self):
        self._check()
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('QEMU machine host deadline expired')
        return remaining

    def _to_native(self, value):
        return _native_time(self._origin_ns + value - self._ctx.session.t_sim_ns,
                            'mapped QEMU target')

    def _to_sim(self, value):
        _native_time(value, 'QEMU source timestamp')
        mapped = value - self._origin_ns + self._ctx.session.t_sim_ns
        if not 0 <= mapped <= 0xffffffffffffffff:
            raise ValueError('QEMU source timestamp precedes the native origin')
        return mapped

    def start(self):
        if self._owner is not None or self._closed:
            raise RuntimeError('QEMU machine can only start once')
        root = resolve_backend_root(self._factory.backend_root)
        try:
            qemu = resolve_qemu(self._factory.qemu or os.environ.get('QEMU_SYSTEM_ARM')
                                or root / 'build/qemu/qemu-system-arm')
        except ValueError as exc:
            raise BackendUnavailable(str(exc)) from exc
        session_class = qmp_session_class()
        sockets = ['qmp', *(['cosim'] if self._imu_connected else []), *self._uarts,
                   *(['uart5'] if self._remote else []),
                   *(port.replace('fdcan', 'can') for port in self._can_ports)]
        command = qemu_command(self._firmware, qemu, sockets,
                               qmp_path=self._factory.qmp_path,
                               gdb_port=self._factory.gdb_port)
        command += ['-accel', 'tcg,thread=single', '-icount', 'shift=0,align=off,sleep=off']
        manager = running_qemu(command, sockets=sockets, startup_timeout=self._remaining())
        self._owner = manager.__enter__()
        self._manager = manager
        self._check()
        remaining = min(2.0, self._remaining())
        self._qmp = session_class(str(self._owner.sockets['qmp']), timeout=remaining,
                                  connect_timeout=remaining, close_timeout=2.0)
        for port in sockets[1:]:
            sock = connect_unix(str(self._owner.sockets[port]), min(2.0, self._remaining()))
            self._peers.append((port, sock))
            stream_type = (StepFramedSocket if port == 'cosim' else
                           CanStream if port.startswith('can') else StreamTransport)
            self._streams[port] = stream_type(sock)
        self._board = QemuBoard(self._boundary)
        commands = self._query('query-commands')
        if not isinstance(commands, list) or not {'x-run-to-virtual-time', 'x-query-virtual-execution'} <= {
                item.get('name') for item in commands if isinstance(item, dict)}:
            raise BackendUnavailable('QEMU lacks required native virtual execution commands')
        properties = self._query('qom-list', {'path': '/machine'})
        required = {'board-state'} | ({'can-execution-state'} if self._can_ports else set())
        if (not isinstance(properties, list) or not required <= {
                item.get('name') for item in properties if isinstance(item, dict)}):
            raise BackendUnavailable('QEMU lacks required native board/CAN observation properties')
        execution = self._query('x-query-virtual-execution')
        if (not isinstance(execution, dict) or execution.get('state') != 'idle'
                or 'target-ns' in execution):
            raise RuntimeError('QEMU must start with idle native execution')
        self._origin_ns = _native_time(execution.get('current-ns'), 'initial QEMU time')
        self._require_paused(initial=True)
        snapshot = self._board.snapshot()
        if snapshot['timestamp_ns'] != self._origin_ns:
            raise RuntimeError('native snapshot and execution origin disagree')
        self._complete_snapshot(snapshot, self._last_time_ns)
        if self._imu_connected:
            self._imu = QemuImuEndpoint(self._streams['cosim'], context=self._ctx,
                                        check=self._check)
            self._imu.start(self._origin_ns)
        for port in self._can_ports:
            wire = port.replace('fdcan', 'can')
            state = observe_initial_paused_can(
                self._boundary, self._streams[wire], timestamp_ns=self._origin_ns,
                controller=f'dm-mc02.{port}', chardev=wire,
                deadline=self._deadline, check=self._check)
            self._can_state[port] = state
        self._ctx.emit('snapshot', 'snapshot', self._snapshot,
                       source_time_ns=self._origin_ns, mapped_time_ns=self._last_time_ns,
                       delivery_time_ns=self._last_time_ns, quality='exact')

    def _require_paused(self, *, initial=False):
        status = self._query('query-status')
        allowed = ('paused', 'prelaunch') if initial else ('paused',)
        if not isinstance(status, dict) or status.get('running') is not False or status.get('status') not in allowed:
            raise RuntimeError('QEMU must remain paused at each completed target')

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
        if self._closed or self._pending is not None or port not in self._digital_samples:
            raise RuntimeError(f'digital source has no completed sample: {self._ctx.node_id}.{port}')
        return self._digital_samples[port]

    def _receive_digital(self, port, level):
        """Deliver a physical line level; external key inputs use pressedness."""
        if type(level) is not int or level not in (0, 1):
            raise ValueError(f'digital input requires integer 0 or 1: {self._ctx.node_id}.{port}')
        self._ctx.check_io()
        try:
            self.input(port, level == 0 if port == 'key' else level)
        except BaseException as error:
            error.add_note(f'digital input failed: {self._ctx.node_id}.{port}')
            raise

    def _validate_cut(self):
        execution = self._query('x-query-virtual-execution')
        initial = self._last_time_ns == self._ctx.session.t_sim_ns
        expected = self._to_native(self._last_time_ns)
        if (not isinstance(execution, dict) or execution.get('current-ns') != expected
                or execution.get('state') != ('idle' if initial else 'reached')
                or (('target-ns' in execution) if initial else execution.get('target-ns') != expected)):
            raise RuntimeError('QEMU completed cut changed outside Simulation control')
        self._require_paused(initial=initial)

    @property
    def connection_info(self):
        """Native debug endpoints and process identity while the owner is live."""
        if self._owner is None:
            raise RuntimeError('QEMU machine has not started')
        return _freeze({'pid': self._owner.process.pid,
                        'start_monotonic_ns': self._owner.start_ns,
                        'sockets': {key: str(value) for key, value in self._owner.sockets.items()},
                        'gdb_port': self._factory.gdb_port,
                        'external_qmp_path': (str(self._factory.qmp_path)
                                              if self._factory.qmp_path else None),
                        'native_origin_ns': self._origin_ns})

    def request_step(self, step):
        self._check()
        if self._owner is None or self._pending is not None:
            raise RuntimeError('QEMU machine is not ready for a target request')
        if (step.session_id != self._ctx.session.session_id or
                step.t_sim_ns - step.dt_ns != self._last_time_ns):
            raise ValueError('QEMU step does not continue the current session')
        self._validate_cut()
        target = self._to_native(step.t_sim_ns)
        if self._imu is not None:
            self._imu.service(self._last_time_ns)
        self._query('x-run-to-virtual-time', {'target-ns': target})
        self._pending = step

    def finish_step(self, step):
        self._check()
        if step is not self._pending:
            raise ValueError('QEMU completion requires the original pending step')
        target = self._to_native(step.t_sim_ns)
        previous = self._to_native(self._last_time_ns)
        while True:
            if self._imu is not None:
                self._imu.service(step.t_sim_ns)
            execution = self._query('x-query-virtual-execution')
            if not isinstance(execution, dict):
                raise TypeError('invalid native execution observation')
            current = _native_time(execution.get('current-ns'), 'native execution time')
            if execution.get('target-ns') != target or not previous <= current <= target:
                raise RuntimeError('native execution target or time changed')
            state = execution.get('state')
            if state == 'reached':
                if current != target:
                    raise RuntimeError('native execution reached an incorrect time')
                break
            if state != 'running':
                raise RuntimeError(f'native execution target was interrupted: {state}')
            previous = current
            select.select([], [], [], min(.001, self._remaining()))
        self._require_paused()
        if self._imu is not None:
            self._imu.service(step.t_sim_ns)
        uart_batches = self._collect(step.t_sim_ns)
        snapshot = self._board.snapshot()
        if snapshot['timestamp_ns'] != target:
            raise RuntimeError('native snapshot moved outside the completed target')
        self._complete_snapshot(snapshot, step.t_sim_ns)
        for port, payload in uart_batches:
            if port in self._uart:
                self._uart[port].send(payload, source_time_ns=target,
                                      mapped_time_ns=step.t_sim_ns, quality='upper_bound')
            self._ctx.emit(port, 'uart', payload, source_time_ns=target,
                           mapped_time_ns=step.t_sim_ns, delivery_time_ns=step.t_sim_ns,
                           quality='upper_bound')
        self._ctx.emit('snapshot', 'snapshot', self._snapshot,
                       source_time_ns=target, mapped_time_ns=step.t_sim_ns,
                       delivery_time_ns=step.t_sim_ns, quality='exact')
        self._require_paused()
        self._check()
        self._last_time_ns = step.t_sim_ns
        self._pending = None
        return StepResult('ok', step.t_sim_ns)

    def _collect(self, cut_ns):
        target = self._to_native(cut_ns)
        for port in self._can_ports:
            wire = port.replace('fdcan', 'can')
            def publish(raw, port=port):
                frame = CanFrame(raw[0], raw[1], raw[2], raw[4], raw[3])
                mapped = self._to_sim(frame.timestamp_ns)
                if mapped > cut_ns:
                    raise RuntimeError('QEMU emitted a CAN frame beyond its completed target')
                # Context owns the earliest delivery cut, never raw wire time.
                self._can[port].send(replace(frame, timestamp_ns=mapped))
                self._ctx.emit(port, 'can-tx', frame,
                               source_time_ns=frame.timestamp_ns, mapped_time_ns=mapped,
                               delivery_time_ns=cut_ns, quality='exact')
            drain_paused_can(self._boundary, self._streams[wire], publish,
                             target_ns=target, controller=f'dm-mc02.{port}', chardev=wire,
                             tx_dropped=self._can_state[port]['tx']['tx_dropped'],
                             deadline=self._deadline, check=self._check)
        uart_batches = []
        for port in self._uarts:
            stream = self._streams[port]
            self._check()
            stream.flush()
            stream.read_once()
            self._check()
            if stream.buffer:
                payload = bytes(stream.buffer)
                stream.buffer.clear()
                uart_batches.append((port, payload))
        return uart_batches

    def _receive_can(self, port, frame):
        self._check()
        if self._pending is not None or self._owner is None:
            raise RuntimeError('CAN input requires a completed paused QEMU cut')
        if not isinstance(frame, CanFrame):
            raise TypeError('QEMU CAN input requires a CanFrame')
        stamp = self._to_native(self._ctx.t_sim_ns)
        wire = port.replace('fdcan', 'can')
        state = self._can_state[port]
        goal = state['rx']['completed_frames'] + 1
        if goal >= 0xffffffffffffffff:
            raise OverflowError('QEMU CAN input receipt counter exhausted')
        stream = self._streams[wire]
        stream.send(frame.can_id, frame.flags, frame.dlc, stamp, frame.data)
        receipt = wait_paused_can_input(
            self._boundary, stream, target_ns=stamp,
            controller=f'dm-mc02.{port}', chardev=wire,
            completed_frames=goal, deadline=self._deadline, check=self._check,
            initial=self._last_time_ns == self._ctx.session.t_sim_ns)
        state['rx'] = receipt
        self._ctx.emit(port, 'can-input-processed', _freeze(receipt),
                       source_time_ns=stamp, mapped_time_ns=self._ctx.t_sim_ns,
                       delivery_time_ns=self._ctx.t_sim_ns, quality='exact')

    def _receive_imu(self, sample):
        self._check()
        self._ctx.check_io()
        if self._pending is not None or self._imu is None:
            raise RuntimeError('IMU input requires a completed paused QEMU cut')
        self._validate_cut()
        # Native time admission precedes any protocol or transport effect.
        self._to_native(self._ctx.t_sim_ns)
        self._imu.receive_imu(sample)

    def input(self, port, payload):
        self._check()
        if self._pending is not None or self._board is None:
            raise RuntimeError('QEMU input requires a completed paused cut')
        self._validate_cut()
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
        self._check()

    def observe(self):
        """Return the last completed immutable native snapshot, without IO."""
        if self._snapshot is None:
            raise RuntimeError('QEMU machine has no completed snapshot')
        return self._snapshot

    def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        if self._imu is not None:
            try:
                self._imu.close()
            except BaseException as exc:
                errors.append(('IMU endpoint', exc))
        for label, peer in reversed(self._peers):
            try:
                peer.close()
            except BaseException as exc:
                errors.append((label, exc))
        if self._qmp is not None:
            try:
                self._qmp.close()
            except BaseException as exc:
                errors.append(('QMP', exc))
        primary = sys.exception() or (errors[0][1] if errors else None)
        if self._manager is not None:
            try:
                self._manager.__exit__(type(primary) if primary else None,
                                       primary, primary.__traceback__ if primary else None)
            except BaseException as exc:
                errors.append(('QEMU process owner', exc))
        if errors:
            primary = errors[0][1]
            for label, exc in errors:
                primary.add_note(f'QEMU cleanup {label} failed: {exc}')
            raise primary
