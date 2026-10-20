"""Machine capability admission and owner composition; native gates use real ELF."""
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.composition import builtin_registry
from sim.fabric.digital import DigitalSample
from sim.machines.qemu import BackendUnavailable, QemuFactory
from sim.machines.qemu import machine as adapter
from sim.runtime import Simulation, SimulationState


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
assert value.port('gpio.H15').direction == 'in'
assert value.port('gpio.H15.out').direction == 'out'
assert 'external_input' not in value.port('gpio.H15.out').capabilities
assert value.port('snapshot').direction == 'out'
assert value.capabilities == frozenset({'machine', 'input'})
assert 'external_input' in value.port('key').capabilities
assert 'external_input' in value.port('gpio.H15').capabilities
assert 'external_input' in value.port('uart1').capabilities
assert 'external_input' not in value.port('fdcan3').capabilities
assert 'external_input' not in value.port('snapshot').capabilities
factory = QemuFactory(qemu='/missing')
spec = NodeSpec('board', 'machine.qemu', {'firmware': sys.argv[1]})
assert factory.validate_input(spec, 'uart1', 'hi\\0é') == b'hi\\0\\xc3\\xa9'
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


@pytest.mark.parametrize('port, payload, expected', [
    ('key', True, True),
    ('gpio.A0', 1, 1),
    ('gpio.H15', 0, 0),
    ('uart1', b'\0bytes', b'\0bytes'),
    ('uart1', 'hi\0é', b'hi\0\xc3\xa9'),
    ('uart1', 'é' * 2048, b'\xc3\xa9' * 2048),
])
def test_input_admission_is_pure_and_returns_canonical_payload(firmware, backend,
                                                             port, payload, expected):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware})
    actual = QemuFactory(qemu='/missing').validate_input(spec, port, payload)
    assert actual == expected and type(actual) is type(expected)
    assert backend.calls == backend.commands == backend.sockets == []


def test_input_admission_does_not_recheck_admitted_firmware(firmware):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware})
    factory = QemuFactory()
    factory.describe(spec)
    firmware.unlink()
    assert factory.validate_input(spec, 'key', True) is True
    assert factory.validate_input(spec, 'uart1', 'é\0') == b'\xc3\xa9\0'


@pytest.mark.parametrize('port, payload, error, match', [
    ('key', 1, TypeError, 'boolean'),
    ('gpio.A0', True, TypeError, 'integer'),
    ('gpio.A0', '1', TypeError, 'integer'),
    ('gpio.A0', 2, ValueError, '0 or 1'),
    ('gpio.A16', 0, ValueError, 'A0 through H15'),
    ('uart1', bytearray(b'mutable'), TypeError, 'bytes or'),
    ('uart1', 'é' * 2049, ValueError, '4096 bytes'),
    ('uart1', b'x' * 4097, ValueError, '4096 bytes'),
    ('uart1', '\ud800', ValueError, 'surrogates'),
    ('uart2', b'unopened', ValueError, 'cannot accept'),
    ('fdcan1', b'unsupported', ValueError, 'cannot accept'),
    ('snapshot', {}, ValueError, 'cannot accept'),
])
def test_invalid_inputs_reject_without_acquiring_native_resources(firmware, backend,
                                                                 port, payload, error, match):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware})
    with pytest.raises(error, match=match):
        QemuFactory().validate_input(spec, port, payload)
    assert backend.calls == backend.commands == backend.sockets == []


@pytest.mark.parametrize('port, payload, owned', [
    ('key', False, 'key'),
    ('key', True, 'gpio.A15'),
    ('gpio.A15', 0, 'key'),
    ('gpio.A0', 1, 'gpio.A0'),
    ('gpio.B0', 0, 'gpio.B0.out'),
    ('key', True, 'gpio.A15.out'),
    ('uart1', b'wire', 'uart1'),
])
def test_wired_inputs_and_physical_aliases_reject_external_injection(firmware, port, payload, owned):
    # Native dm_mc02.c routes user-key and gpio-input A15 to the same input.
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware})
    with pytest.raises(ValueError, match='owned by a connection'):
        QemuFactory().validate_input(spec, port, payload, connected_ports=(owned,))
    assert QemuFactory().validate_input(spec, 'gpio.A14', 0,
                                      connected_ports=(owned,)) == 0


@pytest.mark.parametrize('alias', ['gpio.A15', 'gpio.A15.out'])
def test_topology_rejects_multiple_key_pin_alias_owners_before_creation(firmware, backend, alias):
    factory = QemuFactory()
    wire = SimpleNamespace(
        describe=lambda _spec: NodeDescription((PortSpec('signal', 'digital', category='signal',
                                                       max_connections=None),)),
        create=lambda *_args: pytest.fail('pure admission created a connection'))
    nodes = (NodeSpec('board', 'machine.qemu', {'firmware': firmware}),
             NodeSpec('wire', 'fixture.wire'))
    topology = Topology(nodes, (LinkSpec('board.key', 'wire.signal'),
                               LinkSpec('wire.signal', f'board.{alias}')))
    registry = {'machine.qemu': factory, 'fixture.wire': wire}
    with pytest.raises(ValueError, match='aliases.*share physical pin gpio.A15'):
        topology.validate(registry)
    independent_pins = Topology(nodes, (LinkSpec('board.key', 'wire.signal'),
                                       LinkSpec('wire.signal', 'board.gpio.A14')))
    independent_pins.validate(registry)
    assert backend.calls == backend.commands == backend.sockets == []


@pytest.mark.parametrize('port, protocol', [('spi1', 'spi'), ('i2c1', 'i2c'), ('adc1', 'adc')])
def test_unsupported_interfaces_name_endpoint_and_reject_before_acquisition(firmware, backend, port, protocol):
    # These interfaces have no public execution adapter in the QEMU profile.
    owner = SimpleNamespace(
        describe=lambda _spec: NodeDescription((PortSpec('port', protocol),), {'connection'}),
        create=lambda *_args: pytest.fail('unsupported topology acquired resources'))
    graph = Topology((NodeSpec('board', 'machine.qemu', {'firmware': firmware}),
                      NodeSpec('wire', 'fixture.owner')),
                     (LinkSpec(f'board.{port}', 'wire.port'),))
    with pytest.raises(ValueError) as caught:
        Simulation(graph, {'machine.qemu': QemuFactory(), 'fixture.owner': owner})
    assert str(caught.value) == f"invalid link board.{port}, wire.port: unknown port '{port}'"
    assert backend.calls == backend.commands == backend.sockets == []


@pytest.mark.parametrize('port, protocol, category', [
    ('uart1', 'uart', 'protocol'), ('snapshot', 'snapshot', 'signal'),
])
def test_described_but_unimplemented_connections_reject_explicitly_before_acquisition(
        firmware, backend, port, protocol, category):
    owner = SimpleNamespace(
        describe=lambda _spec: NodeDescription((PortSpec('port', protocol, category=category),),
                                                {'connection'}),
        create=lambda *_args: pytest.fail('unsupported topology acquired resources'))
    graph = Topology((NodeSpec('board', 'machine.qemu', {'firmware': firmware}),
                      NodeSpec('wire', 'fixture.owner')),
                     (LinkSpec(f'board.{port}', 'wire.port'),))
    with pytest.raises(ValueError, match=rf'unsupported QEMU connection: board\.{port} \({protocol}\)'):
        Simulation(graph, {'machine.qemu': QemuFactory(), 'fixture.owner': owner})
    assert backend.calls == backend.commands == backend.sockets == []


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """Substitute acquisition only; all adapter/transport/lifecycle code is real."""
    calls, clients, sockets, commands = [], [], [], []
    state = SimpleNamespace(origin=71, commands_available=True, close_error=None,
                            external_change=False, read_snapshot=None)

    class Qmp:
        def __init__(self, *_args, **_kwargs):
            self.current, self.target = state.origin, None
            self.index, self.key_pressed = len(clients), False
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
                return state.read_snapshot(self) if state.read_snapshot else snapshot(self.current)
            if name == 'x-run-to-virtual-time':
                self.target = self.current = arguments['target-ns']
                return {}
            assert name == 'qom-set'
            if arguments['property'] == 'user-key':
                self.key_pressed = arguments['value']
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


def digital_simulation(firmware):
    options = {'firmware': firmware, 'uarts': ()}
    graph = Topology((NodeSpec('source', 'machine.qemu', options),
                      NodeSpec('receiver', 'machine.qemu', options),
                      NodeSpec('wire', 'net.digital')),
                     (LinkSpec('source.gpio.B0.out', 'wire.signal'),
                      LinkSpec('wire.signal', 'receiver.key'),
                      LinkSpec('receiver.gpio.C5', 'wire.signal')))
    return Simulation(graph, builtin_registry(), quantum_ns=10)


def native_digital_snapshots(client, *, lose_drive_at=None):
    """Fixed native-boundary observations; this fixture does not execute firmware."""
    value = snapshot(client.current)
    cut = client.current - 71
    if client.index == 0:
        value['gpio']['B']['output_mask'] = int(cut > 0 and (lose_drive_at is None or cut < lose_drive_at))
        value['gpio']['B']['sampled'] = int(cut >= 20)
        # The public sampled field is the observation contract, rather than ODR.
        value['gpio']['B']['odr'] = int(cut < 20)
    else:
        level = int(not client.key_pressed)
        value['key'].update(pressed=client.key_pressed, level=level)
        value['gpio']['A']['sampled'] = level << 15
    return value


def test_unified_simulation_samples_completed_gpio_then_drives_physical_key_and_gpio(
        firmware, backend, monkeypatch):
    backend.state.read_snapshot = native_digital_snapshots
    projections = []
    public_projection = adapter.digital_level

    def project(endpoint, value):
        assert isinstance(value, dict)  # Projection precedes immutable publication.
        projections.append((endpoint, value['timestamp_ns']))
        return public_projection(endpoint, value)

    monkeypatch.setattr(adapter, 'digital_level', project)
    with digital_simulation(firmware) as sim:
        assert not any(name == 'qom-set' for name, _ in backend.commands)
        assert sim.components['wire'].observe()['active'] is False
        assert projections == []
        before, events = list(backend.commands), sim.events
        for endpoint, payload in (('receiver.key', True), ('receiver.gpio.A15', 0),
                                  ('receiver.gpio.C5', 1), ('source.gpio.B0', 0)):
            with pytest.raises(ValueError, match='owned by a connection'):
                sim.schedule_input(endpoint, payload)
        with pytest.raises(ValueError, match='cannot accept external'):
            sim.schedule_input('source.gpio.B0.out', 1)
        assert backend.commands == before and sim.events == events
        assert sim.state == SimulationState.PAUSED

        sim.step(10)
        assert projections == [('gpio.B0', 81)]
        assert sim.components['source'].observe()['gpio']['B']['sampled'] == 0
        # Delivery occurs after both native snapshots; the receiver has not run yet.
        assert sim.components['receiver'].observe()['key']['level'] == 1
        assert backend.clients[1].key_pressed is True
        assert ('qom-set', {'path': '/machine', 'property': 'gpio-input', 'value': 'C5=0'}) in backend.commands
        samples = [event for event in sim.events if event.kind == 'net.digital.sample']
        deliveries = [event for event in sim.events if event.kind == 'net.digital.delivery']
        assert samples[-1].payload['source'] == 'source.gpio.B0.out'
        assert [(event.payload['destination'], event.payload['level']) for event in deliveries] == [
            ('receiver.key', 0), ('receiver.gpio.C5', 0)]
        assert all(event.quality == 'sampled' and event.source_time_ns == 81
                   and event.mapped_time_ns == event.delivery_time_ns == 10 for event in deliveries)
        snapshots = [event for event in sim.events if event.kind == 'snapshot' and event.mapped_time_ns == 10]
        assert len(snapshots) == 2 and max(event.sequence for event in snapshots) < samples[-1].sequence
        assert samples[-1].sequence < deliveries[0].sequence

        sim.step(10)
        assert sim.components['receiver'].observe()['key']['level'] == 0
        assert backend.clients[1].key_pressed is False
        sim.step(10)
        assert sim.components['receiver'].observe()['key']['level'] == 1
        assert projections == [('gpio.B0', 81), ('gpio.B0', 91), ('gpio.B0', 101)]
        assert [event.payload['level'] for event in sim.events
                if event.kind == 'net.digital.delivery' and event.payload['destination'] == 'receiver.key'] == [0, 1]


def test_unified_simulation_lost_gpio_drive_names_source_and_freezes_at_last_completed_time(firmware, backend):
    backend.state.read_snapshot = lambda client: native_digital_snapshots(client, lose_drive_at=20)
    with digital_simulation(firmware) as sim:
        sim.step(10)
        with pytest.raises(RuntimeError, match=r'source\.gpio\.B0\.out lost output drive') as caught:
            sim.step(10)
        assert sim.state == SimulationState.FAILED and sim.t_sim_ns == 10
        assert backend.clients[1].key_pressed is True
        assert 'source.gpio.B0.out' in caught.value.__notes__[0]
        before = list(backend.commands)
        with pytest.raises(RuntimeError, match='healthy'):
            sim.step(10)
        assert backend.commands == before


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


def test_rejected_scheduled_inputs_do_not_touch_started_machine_and_allow_recovery(firmware, backend):
    with simulation(firmware) as sim:
        before_commands, before_calls = list(backend.commands), list(backend.calls)
        before_events = sim.events
        for port, payload, error, match in (
            ('fdcan1', b'unsupported', ValueError, 'cannot accept external'),
            ('snapshot', {}, ValueError, 'cannot accept external'),
            ('unknown', 1, ValueError, 'unknown port'),
            ('key', 1, TypeError, 'boolean'),
            ('gpio.A0', 2, ValueError, '0 or 1'),
            ('uart1', 'é' * 2049, ValueError, '4096 bytes'),
        ):
            with pytest.raises(error, match=match):
                sim.schedule_input(f'board.{port}', payload)
            assert backend.commands == before_commands and backend.calls == before_calls
            assert sim.events == before_events
            assert sim.state == SimulationState.PAUSED and sim.failure is None
        sim.schedule_input('board.key', True)
        sim.schedule_input('board.uart1', 'hello\0é')
        sim.step(1000)
        assert ('qom-set', {'path': '/machine', 'property': 'user-key', 'value': True}) in backend.commands
        assert backend.sockets[0][2].recv(4096) == b'hello\0\xc3\xa9'
        delivered = [event.payload for event in sim.events if event.kind == 'input.delivered']
        assert delivered == [True, b'hello\0\xc3\xa9']


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
