"""Generic native QEMU execution; board semantics belong to an injected profile.

This owner alone launches, connects, requests exact native targets and cleans
up. Profiles borrow bounded native access and own their public wire adapters.
"""
from __future__ import annotations

import os
import re
import select
import sys
import time
from types import MappingProxyType

from sim.api import StepResult
from sim.api.topology import immutable as _freeze
from sim.boards.launch import qmp_socket_path, resolve_qemu
from sim.transports import connect_unix

from .deadline import deadline_qmp_call
from .errors import BackendUnavailable
from .process import running_qemu
from .qmp import backend_root as resolve_backend_root, qmp_session_class

_SIGNED64_MAX = 0x7fffffffffffffff


def _native_time(value, name):
    if type(value) is not int or not 0 <= value <= _SIGNED64_MAX:
        raise ValueError(f'{name} must be signed64 nonnegative nanoseconds')
    return value


class QemuFactory:
    """Explicit native backend and pure profile; descriptions acquire no IO."""

    def __init__(self, *, profile=None, qemu=None, backend_root=None, gdb_port=None,
                 qmp_path=None):
        if profile is None:
            from .profile import DmMc02Profile
            profile = DmMc02Profile()
        if any(not callable(getattr(profile, name, None)) for name in
               ('describe', 'validate_input', 'validate_connections', 'create_adapter')):
            raise TypeError('QEMU profile lacks description, admission or adapter methods')
        self.profile = profile
        self.qemu, self.backend_root = qemu, backend_root
        if gdb_port is not None and (type(gdb_port) is not int or not 1 <= gdb_port <= 65535):
            raise ValueError('GDB port must be an integer in 1..65535')
        self.gdb_port = gdb_port
        self.qmp_path = qmp_socket_path(qmp_path)

    def describe(self, spec):
        return self.profile.describe(spec)

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        return self.profile.validate_input(spec, port, payload, connected_ports=connected_ports)

    def validate_inputs(self, spec, entries, *, origin_ns=None):
        validate = getattr(self.profile, 'validate_inputs', None)
        if validate is not None:
            validate(spec, entries, origin_ns=origin_ns)

    def validate_topology(self, topology, descriptions, registry):
        owners = {'gdb_port': {}, 'qmp_path': {}}
        for node in topology.nodes:
            factory = registry[node.kind]
            if not isinstance(factory, QemuFactory):
                continue
            for name, seen in owners.items():
                value = getattr(factory, name)
                if value is None:
                    continue
                if value in seen:
                    raise ValueError(f'QEMU nodes {seen[value]} and {node.name} share {name}: {value}')
                seen[value] = node.name
            factory.profile.validate_connections(node, topology, descriptions)

    def create(self, spec, ctx):
        return QemuMachine(spec, ctx, self)


class QemuMachine:
    """Sole native process/connection/target owner, independent of board ports.

    The profile adapter provides stream_types, launch_command, start, service,
    complete_cut, input, observe and close, plus commit_inputs if advertised.
    It borrows this owner's command,
    deadline, stream and epoch-mapping boundary and must never advance a CPU.
    """

    def __init__(self, spec, ctx, factory):
        self._ctx, self._factory = ctx, factory
        self._manager = self._owner = self._qmp = None
        self._peers = []
        self._streams = {}
        self._origin_ns = None
        self._last_time_ns = ctx.session.t_sim_ns
        self._pending = None
        self._prepared = None
        self._closed = False
        # A profile owns partial construction until it returns an adapter.
        # Once returned, this backend admits and closes that object on failure.
        self._adapter = factory.profile.create_adapter(spec, ctx, self)
        try:
            if any(not callable(getattr(self._adapter, name, None)) for name in
                   ('launch_command', 'start', 'service', 'complete_cut', 'input', 'observe', 'close')):
                raise TypeError('QEMU profile adapter lacks required lifecycle or boundary methods')
            if ('input_commit' in factory.describe(spec).capabilities and
                    not callable(getattr(self._adapter, 'commit_inputs', None))):
                raise TypeError('QEMU profile adapter lacks advertised commit_inputs method')
            self._stream_types = dict(self._adapter.stream_types)
            if (any(type(name) is not str or name == 'qmp'
                    or re.fullmatch(r'[a-z][a-z0-9_]{0,15}', name) is None
                    for name in self._stream_types)
                    or any(not callable(value) for value in self._stream_types.values())):
                raise TypeError('QEMU profile streams need short names and callable types; qmp is reserved')
        except BaseException as primary:
            close = getattr(self._adapter, 'close', None)
            if callable(close):
                try:
                    close()
                except BaseException as error:
                    primary.add_note(f'QEMU profile admission cleanup failed: {error}')
            raise

    @property
    def streams(self):
        return MappingProxyType(self._streams)

    @property
    def native_origin_ns(self):
        return self._origin_ns

    @property
    def t_sim_ns(self):
        return self._last_time_ns

    @property
    def pending(self):
        return self._pending

    @property
    def started(self):
        return self._owner is not None

    @property
    def deadline_monotonic(self):
        return self._ctx.deadline_monotonic

    def check(self):
        if self._closed:
            raise RuntimeError('QEMU machine is closed')
        self._ctx.check_deadline()
        if self._owner is not None and self._owner.process.poll() is not None:
            raise RuntimeError(f'QEMU exited ({self._owner.process.returncode})')

    def _call(self, callback):
        self.check()
        result = deadline_qmp_call(self._qmp, self.deadline_monotonic, 'QEMU machine', callback)
        self.check()
        return result

    def command(self, name, arguments=None):
        return self._call(lambda: self._qmp.command(name, arguments))

    def command_many(self, commands):
        return self._call(lambda: self._qmp.command_many(commands))

    def _remaining(self):
        self.check()
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('QEMU machine host deadline expired')
        return remaining

    def to_native(self, value):
        return _native_time(self._origin_ns + value - self._ctx.session.t_sim_ns,
                            'mapped QEMU target')

    def to_sim(self, value):
        _native_time(value, 'QEMU source timestamp')
        mapped = value - self._origin_ns + self._ctx.session.t_sim_ns
        if not 0 <= mapped <= 0xffffffffffffffff:
            raise ValueError('QEMU source timestamp precedes the native origin')
        return mapped

    def start(self):
        if self._owner is not None or self._closed:
            raise RuntimeError('QEMU machine can only start once')
        value = self._factory.qemu or os.environ.get('QEMU_SYSTEM_ARM')
        if value is None:
            root = resolve_backend_root(self._factory.backend_root)
            if root is None:
                raise BackendUnavailable('QEMU executable is unavailable; supply --qemu, '
                                         'QEMU_SYSTEM_ARM or an initialized --backend-root')
            value = root / 'build/qemu/qemu-system-arm'
        try:
            qemu = resolve_qemu(value)
        except ValueError as exc:
            raise BackendUnavailable(str(exc)) from exc
        session_class = qmp_session_class()
        sockets = ['qmp', *self._stream_types]
        command = self._adapter.launch_command(qemu, sockets,
            qmp_path=self._factory.qmp_path, gdb_port=self._factory.gdb_port)
        command += ['-accel', 'tcg,thread=single', '-icount', 'shift=0,align=off,sleep=off']
        manager = running_qemu(command, sockets=sockets, startup_timeout=self._remaining())
        self._owner = manager.__enter__()
        self._manager = manager
        self.check()
        remaining = min(2.0, self._remaining())
        self._qmp = session_class(str(self._owner.sockets['qmp']), timeout=remaining,
                                  connect_timeout=remaining, close_timeout=2.0)
        for port in sockets[1:]:
            sock = connect_unix(str(self._owner.sockets[port]), min(2.0, self._remaining()))
            self._peers.append((port, sock))
            self._streams[port] = self._stream_types[port](sock)
        commands = self.command('query-commands')
        if not isinstance(commands, list) or not {'x-run-to-virtual-time', 'x-query-virtual-execution'} <= {
                item.get('name') for item in commands if isinstance(item, dict)}:
            raise BackendUnavailable('QEMU lacks required native virtual execution commands')
        execution = self.command('x-query-virtual-execution')
        if (not isinstance(execution, dict) or execution.get('state') != 'idle'
                or 'target-ns' in execution):
            raise RuntimeError('QEMU must start with idle native execution')
        self._origin_ns = _native_time(execution.get('current-ns'), 'initial QEMU time')
        self.require_paused(initial=True)
        self._adapter.start()

    def require_paused(self, *, initial=False):
        status = self.command('query-status')
        allowed = ('paused', 'prelaunch') if initial else ('paused',)
        if not isinstance(status, dict) or status.get('running') is not False or status.get('status') not in allowed:
            raise RuntimeError('QEMU must remain paused at each completed target')

    def validate_cut(self):
        execution, status = self._execution_observation()
        initial = self._last_time_ns == self._ctx.session.t_sim_ns
        expected = self.to_native(self._last_time_ns)
        if (not isinstance(execution, dict) or execution.get('current-ns') != expected
                or execution.get('state') != ('idle' if initial else 'reached')
                or (('target-ns' in execution) if initial else execution.get('target-ns') != expected)):
            raise RuntimeError('QEMU completed cut changed outside Simulation control')
        self._validate_status(status, initial=initial)

    @staticmethod
    def _validate_status(status, *, initial=False):
        allowed = ('paused', 'prelaunch') if initial else ('paused',)
        if (not isinstance(status, dict) or status.get('running') is not False
                or status.get('status') not in allowed):
            raise RuntimeError('QEMU must remain paused at each completed target')

    def _execution_observation(self):
        observe = getattr(self._adapter, 'execution_observation', None)
        if callable(observe):
            return observe()
        return self.command('x-query-virtual-execution'), self.command('query-status')

    @property
    def connection_info(self):
        if self._owner is None:
            raise RuntimeError('QEMU machine has not started')
        return _freeze({'pid': self._owner.process.pid,
                        'start_monotonic_ns': self._owner.start_ns,
                        'sockets': {key: str(value) for key, value in self._owner.sockets.items()},
                        'gdb_port': self._factory.gdb_port,
                        'external_qmp_path': (str(self._factory.qmp_path)
                                              if self._factory.qmp_path else None),
                        'native_origin_ns': self._origin_ns})

    def prepare_step(self, step):
        self.check()
        if self._owner is None or self._pending is not None or self._prepared is not None:
            raise RuntimeError('QEMU machine is not ready for a target request')
        if (step.session_id != self._ctx.session.session_id or
                step.t_sim_ns - step.dt_ns != self._last_time_ns):
            raise ValueError('QEMU step does not continue the current session')
        self.validate_cut()
        self.to_native(step.t_sim_ns)
        self._prepared = step

    def request_step(self, step):
        self.check()
        if step is not self._prepared or self._pending is not None:
            raise ValueError('QEMU request requires the original prepared step')
        target = self.to_native(step.t_sim_ns)
        self._adapter.service(self._last_time_ns)
        self.command('x-run-to-virtual-time', {'target-ns': target})
        self._pending = step
        self._prepared = None

    def finish_step(self, step):
        self.check()
        if step is not self._pending:
            raise ValueError('QEMU completion requires the original pending step')
        target = self.to_native(step.t_sim_ns)
        previous = self.to_native(self._last_time_ns)
        while True:
            self._adapter.service(step.t_sim_ns)
            execution, status = self._execution_observation()
            if not isinstance(execution, dict):
                raise TypeError('invalid native execution observation')
            current = _native_time(execution.get('current-ns'), 'native execution time')
            if execution.get('target-ns') != target or not previous <= current <= target:
                raise RuntimeError('native execution target or time changed')
            state = execution.get('state')
            if state == 'reached':
                if current != target:
                    raise RuntimeError('native execution reached an incorrect time')
                self._validate_status(status)
                break
            if state != 'running':
                raise RuntimeError(f'native execution target was interrupted: {state}')
            previous = current
            select.select([], [], [], min(.001, self._remaining()))
        self._adapter.service(step.t_sim_ns)
        self._adapter.complete_cut(step.t_sim_ns, target)
        self.require_paused()
        self.check()
        self._last_time_ns = step.t_sim_ns
        self._pending = None
        return StepResult('ok', step.t_sim_ns)

    def input(self, port, payload):
        self.check()
        if self._pending is not None or self._owner is None:
            raise RuntimeError('QEMU input requires a completed paused cut')
        self.validate_cut()
        self._adapter.input(port, payload)
        self.check()

    def commit_inputs(self, cut_ns):
        self.check()
        if self._pending is not None or self._owner is None:
            raise RuntimeError('QEMU input commit requires a completed paused cut')
        self._adapter.commit_inputs(cut_ns)
        self.check()

    def observe(self):
        return self._adapter.observe()

    def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        try:
            self._adapter.close()
        except BaseException as exc:
            errors.append(('profile adapter', exc))
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
