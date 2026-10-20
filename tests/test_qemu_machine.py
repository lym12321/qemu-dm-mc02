"""Machine capability admission and owner composition; native gates use real ELF."""
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sim.api import NodeSpec, Topology
from sim.machines.qemu import BackendUnavailable, QemuFactory
from sim.machines.qemu import machine as adapter
from sim.runtime import Simulation


def snapshot(timestamp):
    return {'schema': 1, 'timestamp_ns': timestamp, 'board': 'DM-MC02',
            'vin_mv': 24000,
            'gpio': {bank: {'moder': 0, 'sampled': 0, 'odr': 0, 'output_mask': 0}
                     for bank in 'ABCDEFGH'},
            'led': {'pin': 'A7', 'rgb': 0, 'brightness': 0, 'source': 'gpio-latch'},
            'key': {'pin': 'A15', 'pressed': False, 'level': 1}}


@pytest.fixture
def firmware(tmp_path):
    image = tmp_path / 'guest.elf'
    image.write_bytes(b'input fixture')
    return image


def test_description_is_static_and_does_not_import_native_client(firmware):
    code = '''
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    level = kwargs.get('level', args[3] if len(args) > 3 else 0)
    if not level and name.startswith(('qemu', 'tools', 'sim.robots', 'sim.arena', 'sim.lab', 'sim.physics')):
        raise AssertionError('unexpected backend dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from sim.api import NodeSpec
from sim.machines.qemu import QemuFactory
value = QemuFactory(qemu='/missing').describe(NodeSpec('board', 'machine.qemu', {'firmware': sys.argv[1]}))
assert value.port('fdcan3').protocol == 'can'
assert value.port('gpio.H15').category == 'signal'
assert value.port('snapshot').direction == 'out'
assert value.capabilities == frozenset({'machine', 'input'})
assert not any(name.startswith('qemu.qmp') for name in sys.modules)
'''
    subprocess.run([sys.executable, '-c', code, str(firmware)], check=True,
                   cwd=Path(__file__).resolve().parents[1], timeout=5)


@pytest.mark.parametrize('options, match', [
    ({'model': 'other'}, 'model'),
    ({'uarts': ['uart1', 'uart1']}, 'UART'),
    ({'uarts': ['uart4']}, 'UART'),
    ({'uarts': 'uart1'}, 'list'),
    ({'unknown': True}, 'unknown'),
])
def test_invalid_options_reject_before_native_acquisition(firmware, options, match):
    with pytest.raises(ValueError, match=match):
        QemuFactory().describe(NodeSpec('board', 'machine.qemu', {'firmware': firmware, **options}))


def test_firmware_resolves_against_manifest_directory(firmware):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware.name}, base_dir=firmware.parent)
    assert adapter.admit_options(spec)[0] == firmware
    description = QemuFactory().describe(NodeSpec('board', 'machine.qemu',
                                                 {'firmware': firmware, 'uarts': []}))
    with pytest.raises(ValueError, match='unknown port'):
        description.port('uart1')


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """Substitute acquisition only; all adapter/transport/lifecycle code is real."""
    calls, clients, sockets, commands = [], [], [], []
    state = SimpleNamespace(origin=71, commands_available=True, close_error=None,
                            external_change=False)

    class Qmp:
        def __init__(self, *_args, **_kwargs):
            self.current, self.target = state.origin, None
            self.timeouts = []
            clients.append(self)

        def settimeout(self, value):
            self.timeouts.append(value)

        def command(self, name, arguments=None):
            commands.append((name, arguments))
            if name == 'query-commands':
                return [{'name': name} for name in (
                    'x-run-to-virtual-time', 'x-query-virtual-execution')] if state.commands_available else []
            if name == 'qom-list':
                assert arguments == {'path': '/machine'}
                return [{'name': 'board-state'}, {'name': 'can-execution-state'}]
            if name == 'x-query-virtual-execution':
                result = {'state': 'idle' if self.target is None else 'reached', 'current-ns': self.current}
                if self.target is not None:
                    result['target-ns'] = self.target
                if state.external_change:
                    result['state'] = 'cancelled'
                return result
            if name == 'query-status':
                return {'running': False, 'status': 'prelaunch' if self.target is None else 'paused'}
            if name == 'qom-get':
                assert arguments == {'path': '/machine', 'property': 'board-state'}
                return snapshot(self.current)
            if name == 'x-run-to-virtual-time':
                self.target = self.current = arguments['target-ns']
                return {}
            assert name == 'qom-set'
            return {}

        def close(self):
            calls.append('QMP close')
            if state.close_error:
                raise state.close_error

    @contextmanager
    def owner(command, *, sockets, startup_timeout):
        calls.append(('start', command, tuple(sockets), startup_timeout))
        paths = {name: tmp_path / f'{name}.sock' for name in sockets}
        try:
            yield SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=123,
                                                          returncode=None),
                                  start_ns=100, sockets=paths)
        finally:
            calls.append('process close')

    def connect(path, _timeout):
        host, peer = socket.socketpair()
        host.setblocking(False)
        peer.setblocking(False)
        sockets.append((Path(path).stem, host, peer))
        return host

    monkeypatch.setattr(adapter, 'resolve_backend_root', lambda _root: tmp_path)
    monkeypatch.setattr(adapter, 'resolve_qemu', lambda value: Path(value))
    monkeypatch.setattr(adapter, 'qmp_session_class', lambda _root: Qmp)
    monkeypatch.setattr(adapter, 'running_qemu', owner)
    monkeypatch.setattr(adapter, 'connect_unix', connect)
    yield SimpleNamespace(calls=calls, clients=clients, sockets=sockets, commands=commands, state=state)
    for _, host, peer in sockets:
        host.close()
        peer.close()


def simulation(firmware, **options):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware, **options})
    return Simulation(Topology((spec,)), {'machine.qemu': QemuFactory()})


def test_native_target_mapping_uart_bound_and_immutable_observation(firmware, backend):
    sim = simulation(firmware)
    sim.start()
    machine = sim.components['board']
    info = machine.connection_info
    assert info['pid'] == 123 and info['native_origin_ns'] == 71
    assert tuple(info['sockets']) == ('qmp', 'uart1')
    command = backend.calls[0][1]
    assert '-S' in command
    assert command[command.index('-accel') + 1] == 'tcg,thread=single'
    assert command[command.index('-icount') + 1] == 'shift=0,align=off,sleep=off'
    _, host, peer = backend.sockets[0]
    peer.sendall(b'a' * 4096 + b'TAIL')
    sim.step(1000)
    assert ('x-run-to-virtual-time', {'target-ns': 1071}) in backend.commands
    uart = [event for event in sim.events if event.kind == 'uart']
    assert len(uart) == 1 and uart[0].payload == b'a' * 4096
    assert (uart[0].source_time_ns, uart[0].mapped_time_ns,
            uart[0].delivery_time_ns, uart[0].quality) == (1071, 1000, 1000, 'upper_bound')
    with pytest.raises(TypeError):
        machine.observe()['gpio']['A']['sampled'] = 1
    before = len(backend.commands)
    assert machine.observe()['timestamp_ns'] == 1071
    assert len(backend.commands) == before
    sim.step(1000)
    assert [event.payload for event in sim.events if event.kind == 'uart'] == [b'a' * 4096, b'TAIL']
    sim.close()
    sim.close()
    assert host.fileno() == -1
    assert backend.calls[-2:] == ['QMP close', 'process close']


def test_inputs_only_change_admitted_public_board_ports(firmware, backend):
    sim = simulation(firmware)
    sim.start()
    sim.schedule_input('board.key', True)
    sim.schedule_input('board.gpio.A0', 1)
    sim.schedule_input('board.uart1', b'hello')
    sim.step(1000)
    assert ('qom-set', {'path': '/machine', 'property': 'user-key', 'value': True}) in backend.commands
    assert ('qom-set', {'path': '/machine', 'property': 'gpio-input', 'value': 'A0=1'}) in backend.commands
    assert backend.sockets[0][2].recv(4096) == b'hello'
    sim.close()


def test_external_run_control_invalidates_machine_cut_before_io(firmware, backend):
    sim = simulation(firmware)
    sim.start()
    backend.state.external_change = True
    sim.schedule_input('board.key', True)
    with pytest.raises(RuntimeError, match='outside Simulation control'):
        sim.step()
    assert not any(name in ('qom-set', 'x-run-to-virtual-time') for name, _ in backend.commands)
    sim.close()


def test_native_capability_failure_closes_all_acquired_resources(firmware, backend):
    backend.state.commands_available = False
    sim = simulation(firmware)
    with pytest.raises(BackendUnavailable, match='virtual execution'):
        sim.start()
    assert backend.sockets[0][1].fileno() == -1
    assert backend.calls[-2:] == ['QMP close', 'process close']
    sim.close()


def test_native_target_overflow_does_not_submit_request(firmware, backend):
    backend.state.origin = 0x7fffffffffffffff
    sim = simulation(firmware)
    sim.start()
    with pytest.raises(ValueError, match='signed64'):
        sim.step(1)
    assert not any(name == 'x-run-to-virtual-time' for name, _ in backend.commands)
    assert sim.t_sim_ns == 0
    sim.close()


def test_qmp_close_failure_still_releases_process_owner(firmware, backend):
    sim = simulation(firmware)
    sim.start()
    backend.state.close_error = OSError('close failed')
    with pytest.raises(OSError, match='close failed'):
        sim.close()
    assert backend.calls[-2:] == ['QMP close', 'process close']
    assert backend.sockets[0][1].fileno() == -1


@pytest.mark.parametrize('endpoint', ['gdb', 'qmp'])
@pytest.mark.parametrize('shared_factory', [True, False])
def test_debug_endpoint_collisions_reject_before_resource_creation(firmware, tmp_path, endpoint, shared_factory):
    options = {'gdb_port': 3333} if endpoint == 'gdb' else {'qmp_path': tmp_path / 'debug.sock'}
    first = QemuFactory(**options)
    second = first if shared_factory else QemuFactory(**options)
    topology = Topology((NodeSpec('first', 'first.qemu', {'firmware': firmware}),
                         NodeSpec('second', 'second.qemu', {'firmware': firmware})))
    with pytest.raises(ValueError, match='share'):
        topology.validate({'first.qemu': first, 'second.qemu': second})


def test_qemu_executable_environment_override(firmware, backend, monkeypatch):
    monkeypatch.setenv('QEMU_SYSTEM_ARM', '/explicit/native-qemu')
    sim = simulation(firmware)
    sim.start()
    assert backend.calls[0][1][0] == '/explicit/native-qemu'
    sim.close()


def test_machine_uses_shared_startup_budget_above_ten_seconds(firmware, backend):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware})
    sim = Simulation(Topology((spec,)), {'machine.qemu': QemuFactory()},
                     startup_timeout_s=20, host_timeout_s=30)
    sim.start()
    sim.reset()
    starts = [call for call in backend.calls if isinstance(call, tuple) and call[0] == 'start']
    assert len(starts) == 2
    assert all(10 < call[3] <= 20 for call in starts)
    sim.close()
