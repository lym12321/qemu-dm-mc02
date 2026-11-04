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
from sim.machines.qemu import profile as dm_profile
from sim.runtime import Simulation, SimulationState
from sim.transports import StreamTransport


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
assert value.port('imu').protocol == 'imu'
assert value.port('imu').category == 'signal'
assert value.port('imu').direction == 'in'
assert 'external_input' not in value.port('imu').capabilities
assert value.port('gpio.H15').category == 'signal'
assert value.port('gpio.H15').direction == 'in'
assert value.port('gpio.H15.out').direction == 'out'
assert 'external_input' not in value.port('gpio.H15.out').capabilities
assert value.port('snapshot').direction == 'out'
assert value.capabilities == frozenset({'machine', 'input', 'input_commit'})
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
    ({'uarts': ['uart5']}, 'remote'),
    ({'uarts': 'uart1'}, 'list'),
    ({'remote': 1}, 'boolean'),
    ({'unknown': True}, 'unknown'),
])
def test_invalid_options_reject_before_native_acquisition(firmware, options, match):
    with pytest.raises(ValueError, match=match):
        QemuFactory().describe(NodeSpec('board', 'machine.qemu', {'firmware': firmware, **options}))


def test_firmware_resolves_against_manifest_directory(firmware):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware.name}, base_dir=firmware.parent)
    assert dm_profile.admit_options(spec)[0] == firmware
    description = QemuFactory().describe(NodeSpec('board', 'machine.qemu',
                                                 {'firmware': firmware, 'uarts': []}))
    with pytest.raises(ValueError, match='unknown port'):
        description.port('uart1')


def test_description_does_not_access_firmware_and_execution_rechecks_it(firmware, backend, monkeypatch):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware})
    factory = QemuFactory()
    firmware.unlink()
    with monkeypatch.context() as guard:
        guard.setattr(Path, 'stat', lambda *_args, **_kwargs: pytest.fail('description accessed filesystem'))
        assert factory.describe(spec).port('uart1').protocol == 'uart'
        assert factory.validate_input(spec, 'key', True) is True
    sim = Simulation(Topology((spec,)), {'machine.qemu': factory})
    with pytest.raises(ValueError, match='firmware is not readable'):
        sim.start()
    assert sim.state == SimulationState.FAILED
    assert backend.calls == backend.commands == backend.sockets == []
    sim.close()


def test_injected_profile_imports_no_board_implementation():
    code = '''
import importlib.abc, sys
from types import SimpleNamespace
class NoBoard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sim.boards' or fullname.startswith('sim.boards.') or fullname == 'sim.machines.qemu.profile':
            raise AssertionError('unexpected board dependency: ' + fullname)
sys.meta_path.insert(0, NoBoard())
from sim.api import NodeDescription, NodeSpec
from sim.machines.qemu import QemuFactory
profile = SimpleNamespace(describe=lambda _: NodeDescription(capabilities={'machine'}),
    validate_input=lambda *a, **k: None, validate_connections=lambda *a: None,
    create_adapter=lambda *a: None)
assert QemuFactory(profile=profile).describe(NodeSpec('cpu', 'fixture')).capabilities == {'machine'}
'''
    subprocess.run([sys.executable, '-c', code], check=True, timeout=5)


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


def test_adc_ports_and_input_batches_are_pure_and_opt_in(firmware, backend):
    factory = QemuFactory(qemu='/missing')
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware, 'adc': True})
    description = factory.describe(spec)
    assert [p.name for p in description.ports if p.protocol == 'adc'] == [
        f'adc1.ch{channel}' for channel in range(32)]
    assert description.port('adc1.ch31').direction == 'in'
    assert factory.validate_input(spec, 'adc1.ch0', {'raw': 65535}) == {'raw': 65535}
    assert factory.validate_input(spec, 'adc1.ch31', {'voltage_uv': 3300000}) == {'voltage_uv': 3300000}
    factory.validate_inputs(spec, ((0, 'adc1.ch0', {'raw': 1}),), origin_ns=None)
    factory.validate_inputs(spec, ((0, 'adc1.ch0', {'raw': 1}),), origin_ns=7)
    with pytest.raises(ValueError, match='session origin'):
        factory.validate_inputs(spec, ((0, 'adc1.ch0', {'raw': 1}),), origin_ns=0)
    with pytest.raises(ValueError, match='duplicate ADC'):
        factory.validate_inputs(spec, ((1, 'adc1.ch0', {'raw': 1}),
                                       (1, 'adc1.ch0', {'voltage_uv': 2})), origin_ns=0)
    with pytest.raises(ValueError, match='capacity per cut is 16'):
        factory.validate_inputs(spec, tuple((1, f'adc1.ch{i}', {'raw': i}) for i in range(17)))
    with pytest.raises(ValueError, match='cannot accept'):
        factory.validate_input(NodeSpec('board', 'machine.qemu', {'firmware': firmware}),
                               'adc1.ch0', {'raw': 1})
    assert backend.calls == backend.commands == backend.sockets == []


@pytest.mark.parametrize('payload,error', [
    ({}, ValueError), ({'raw': 1, 'voltage_uv': 2}, ValueError),
    ({'other': 1}, ValueError), ({'raw': True}, TypeError),
    ({'raw': 1.0}, TypeError), ({'raw': -1}, ValueError),
    ({'raw': 65536}, ValueError), ({'voltage_uv': 3300001}, ValueError)])
def test_adc_invalid_payloads_reject_before_native_acquisition(firmware, backend, payload, error):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware, 'adc': True})
    with pytest.raises(error):
        QemuFactory().validate_input(spec, 'adc1.ch0', payload)
    assert backend.calls == backend.commands == backend.sockets == []


def test_adc_network_links_reject_before_acquisition(firmware, backend):
    owner = SimpleNamespace(
        describe=lambda _: NodeDescription((PortSpec('signal', 'adc', category='signal'),),
                                            {'connection'}),
        create=lambda *_: pytest.fail('ADC links must reject before acquisition'))
    graph = Topology((NodeSpec('board', 'machine.qemu', {'firmware': firmware, 'adc': True}),
                      NodeSpec('wire', 'fixture.owner')),
                     (LinkSpec('board.adc1.ch0', 'wire.signal'),))
    with pytest.raises(ValueError, match='unsupported QEMU connection'):
        Simulation(graph, {'machine.qemu': QemuFactory(), 'fixture.owner': owner})
    assert backend.calls == backend.commands == backend.sockets == []


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
    ('snapshot', 'snapshot', 'signal'),
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
    calls, clients, sockets, commands, groups = [], [], [], [], []
    state = SimpleNamespace(origin=71, origins=None, processes=[], commands_available=True,
                            close_error=None, external_change=False, read_snapshot=None,
                            read_status=None)

    class Qmp:
        def __init__(self, *_args, **_kwargs):
            self.retain_events = _kwargs['retain_events']
            self.current = state.origin if state.origins is None else state.origins[len(clients)]
            self.target = None
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
                if state.read_status is not None:
                    return state.read_status(self)
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

        def command_many(self, requests):
            groups.append(requests)
            return tuple(self.command(name, arguments) for name, arguments in requests)

        def close(self):
            calls.append('QMP close')
            if state.close_error:
                raise state.close_error

    @contextmanager
    def owner(command, *, sockets, startup_timeout):
        calls.append(('start', command, tuple(sockets), startup_timeout))
        paths = {name: tmp_path / str(len(state.processes)) / f'{name}.sock' for name in sockets}
        process = SimpleNamespace(pid=123 + len(state.processes), returncode=None)
        process.poll = lambda: process.returncode
        state.processes.append(process)
        try:
            yield SimpleNamespace(process=process, start_ns=100, sockets=paths)
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
    monkeypatch.setattr(adapter, 'qmp_session_class', lambda: Qmp)
    monkeypatch.setattr(adapter, 'running_qemu', owner)
    monkeypatch.setattr(adapter, 'connect_unix', connect)
    yield SimpleNamespace(calls=calls, clients=clients, sockets=sockets, commands=commands,
                         groups=groups, state=state)
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
    public_projection = dm_profile.digital_level

    def project(endpoint, value):
        assert isinstance(value, dict)  # Projection precedes immutable publication.
        projections.append((endpoint, value['timestamp_ns']))
        return public_projection(endpoint, value)

    monkeypatch.setattr(dm_profile, 'digital_level', project)
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
    assert backend.clients[0].retain_events is False
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


def test_connected_uart_uses_existing_single_reader_and_independent_python_echo(firmware, backend):
    graph = Topology((NodeSpec('board', 'machine.qemu', {'firmware': firmware}),
                      NodeSpec('wire', 'net.uart'), NodeSpec('echo', 'device.uart-echo')),
        (LinkSpec('board.uart1', 'wire.uart'), LinkSpec('echo.uart', 'wire.uart')))
    with Simulation(graph, builtin_registry(), quantum_ns=1000) as sim:
        _, host, peer = backend.sockets[0]
        assert tuple(sim.components['board'].connection_info['sockets']) == ('qmp', 'uart1')
        peer.sendall(b'\x00\x7f\x80\xff')
        sim.step(1000)
        assert peer.recv(4096) == b'\x00\x7f\x80\xff'
        rows = [row for row in sim.events if row.kind == 'net.uart.delivery']
        original = next(row for row in rows if row.payload['source'] == 'board.uart1')
        assert (original.source_time_ns, original.mapped_time_ns,
                original.delivery_time_ns, original.quality) == (1071, 1000, 1000, 'upper_bound')
        echoed = next(row for row in rows if row.payload['source'] == 'echo.uart')
        assert (echoed.source_time_ns, echoed.mapped_time_ns,
                echoed.delivery_time_ns, echoed.quality) == (None, 1000, 1000, 'sampled')
        with pytest.raises(ValueError, match='owned by a connection'):
            sim.schedule_input('board.uart1', b'external')
    assert host.fileno() == -1


def test_remote_is_explicit_raw_input_only_and_maps_to_native_uart5(firmware, backend):
    spec = NodeSpec('board', 'machine.qemu', {'firmware': firmware, 'uarts': [], 'remote': True})
    factory = QemuFactory()
    description = factory.describe(spec)
    remote = description.port('remote')
    assert remote.direction == 'in' and 'external_input' in remote.capabilities
    with pytest.raises(ValueError, match='unknown port'):
        description.port('uart5')
    assert factory.validate_input(spec, 'remote', b'\x00\xff') == b'\x00\xff'
    for value, error in [('text', TypeError), (bytearray(b'x'), TypeError),
                          (b'x' * 4097, ValueError)]:
        with pytest.raises(error):
            factory.validate_input(spec, 'remote', value)
    with Simulation(Topology((spec,), ()), {'machine.qemu': factory}) as sim:
        assert tuple(sim.components['board'].connection_info['sockets']) == ('qmp', 'uart5')
        sim.schedule_input('board.remote', b'')
        sim.schedule_input('board.remote', b'\x00\xff')
        sim.step(1000)
        assert backend.sockets[0][2].recv(4096) == b'\x00\xff'
        assert not any(row.kind == 'uart' for row in sim.events)
    with pytest.raises(ValueError, match='cannot accept'):
        factory.validate_input(NodeSpec('board', 'machine.qemu', {'firmware': firmware}),
                               'remote', b'x')


def test_remote_cannot_attach_to_uart_net_before_native_acquisition(firmware, backend):
    graph = Topology((NodeSpec('board', 'machine.qemu', {'firmware': firmware, 'remote': True}),
                      NodeSpec('wire', 'net.uart'), NodeSpec('echo', 'device.uart-echo')),
        (LinkSpec('board.remote', 'wire.uart'), LinkSpec('echo.uart', 'wire.uart')))
    with pytest.raises(ValueError, match='protocol mismatch'):
        Simulation(graph, builtin_registry())
    assert backend.calls == backend.commands == backend.sockets == []


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


def imu_simulation(firmware, actions, *, adc=False):
    """A completed SI producer; the protocol endpoint is isolated separately."""
    from sim.api import ImuSample, ImuSignalSample, StepResult

    class Source:
        def __init__(self, context):
            self.context, self.cut = context, 0
            context.attach('out', self)

        def read_imu(self):
            actions.append(('source', self.cut))
            return ImuSignalSample(ImuSample((1., -2., 3.), (0., 0., 9.81)), None, self.cut)

        def step(self, step):
            self.cut = step.t_sim_ns
            actions.append(('world', self.cut))
            return StepResult('ok', self.cut)

        def close(self):
            actions.append(('source close', self.cut))

    source = SimpleNamespace(
        describe=lambda _: NodeDescription((PortSpec('out', 'imu', 'out', 'signal'),), {'step'}),
        create=lambda _, context: Source(context))
    registry = {**builtin_registry(), 'fixture.imu': source}
    graph = Topology((NodeSpec('source', 'fixture.imu'),
                      NodeSpec('board', 'machine.qemu', {'firmware': firmware, 'adc': adc}),
                      NodeSpec('wire', 'net.imu')),
                     (LinkSpec('source.out', 'wire.signal'), LinkSpec('wire.signal', 'board.imu')))
    return Simulation(graph, registry, quantum_ns=10, startup_timeout_s=20)


def recording_imu_endpoint(backend, actions, *, start_error=None, service_error=None):
    """Replace only the independently tested protocol conversation."""
    class Endpoint:
        def __init__(self, stream, *, context, check, imu, adc):
            assert isinstance(stream, dm_profile.StepFramedSocket)
            self.stream, self.context, self.check = stream, context, check
            self.imu, self.adc = imu, adc

        def start(self, origin):
            self.check()
            actions.append(('imu start', origin, self.context.deadline_monotonic))
            if start_error:
                raise start_error

        def receive_imu(self, sample):
            self.check()
            actions.append(('imu receive', sample.mapped_time_ns, backend.clients[-1].current))

        def receive_adc(self, channel, payload):
            self.check()
            actions.append(('adc receive', channel, dict(payload), self.context.t_sim_ns))

        def commit_inputs(self, cut):
            self.check()
            actions.append(('imu commit', cut, backend.clients[-1].current))

        def service(self, cut):
            self.check()
            actions.append(('imu service', cut))
            if service_error and cut == 7:
                raise service_error

        def close(self):
            actions.append(('imu close', self.stream.sock.fileno()))

    return Endpoint


def test_connected_imu_uses_one_owned_cosim_and_completed_cut_delivery(firmware, backend, monkeypatch):
    actions = []
    monkeypatch.setattr(dm_profile, 'QemuCosimEndpoint', recording_imu_endpoint(backend, actions))
    sim = imu_simulation(firmware, actions)
    with sim:
        machine = sim.components['board']
        assert tuple(machine.connection_info['sockets']) == ('qmp', 'cosim', 'uart1')
        assert actions[0][:2] == ('imu start', 71)
        # start() shares the startup deadline; successful startup then exposes
        # the longer active deadline through the same live context property.
        assert sim._deadline - actions[0][2] == sim.host_timeout_s - sim.startup_timeout_s
        assert actions[1:] == [('source', 0), ('imu receive', 0, 71), ('imu commit', 0, 71)]
        assert not any(name == 'x-run-to-virtual-time' for name, _ in backend.commands)
        with pytest.raises(ValueError, match='cannot accept external'):
            sim.schedule_input('board.imu', (1, 2, 3))
        actions.clear()
        sim.step(7)
        assert actions == [('imu commit', 0, 71), ('imu service', 0), ('imu service', 7), ('imu service', 7),
                           ('world', 7), ('source', 7), ('imu receive', 7, 78),
                           ('imu commit', 7, 78)]
        actions.clear()
        sim.step(3)
        assert actions[-4:] == [('world', 10), ('source', 10), ('imu receive', 10, 81),
                                ('imu commit', 10, 81)]
    assert actions[-2][0] == 'imu close' and actions[-2][1] >= 0
    assert actions[-1] == ('source close', 10)
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)
    assert backend.calls[-2:] == ['QMP close', 'process close']


def test_unconnected_imu_does_not_construct_endpoint_or_open_cosim(firmware, backend, monkeypatch):
    monkeypatch.setattr(dm_profile, 'QemuCosimEndpoint', lambda *args, **kwargs: pytest.fail('unused co-sim'))
    with simulation(firmware) as sim:
        sim.step(7)
        assert tuple(sim.components['board'].connection_info['sockets']) == ('qmp', 'uart1')


def test_imu_start_failure_preserves_primary_and_closes_all_owned_resources(firmware, backend, monkeypatch):
    actions, primary = [], OSError('co-sim handshake failed')
    monkeypatch.setattr(dm_profile, 'QemuCosimEndpoint', recording_imu_endpoint(
        backend, actions, start_error=primary))
    sim = imu_simulation(firmware, actions)
    with pytest.raises(OSError) as caught:
        sim.start()
    assert caught.value is sim.failure is primary
    assert sim.state == SimulationState.FAILED
    assert any(action[0] == 'imu close' for action in actions)
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)
    assert backend.calls[-2:] == ['QMP close', 'process close']
    assert not any(name == 'x-run-to-virtual-time' for name, _ in backend.commands)
    sim.close()


def test_imu_service_failure_stops_before_world_step_and_never_replays(firmware, backend, monkeypatch):
    actions, primary = [], OSError('co-sim receive failed')
    monkeypatch.setattr(dm_profile, 'QemuCosimEndpoint', recording_imu_endpoint(
        backend, actions, service_error=primary))
    with imu_simulation(firmware, actions) as sim:
        actions.clear()
        with pytest.raises(OSError) as caught:
            sim.step(7)
        assert caught.value is sim.failure is primary
        assert sim.state == SimulationState.FAILED and sim.t_sim_ns == 0
        assert backend.clients[0].current == 78  # Native work already happened.
        assert actions == [('imu commit', 0, 71), ('imu service', 0), ('imu service', 7)]
        before = list(backend.commands)
        with pytest.raises(RuntimeError, match='healthy'):
            sim.step(7)
        assert backend.commands == before


def test_imu_reset_recreates_connection_and_native_origin(firmware, backend, monkeypatch):
    actions = []
    monkeypatch.setattr(dm_profile, 'QemuCosimEndpoint', recording_imu_endpoint(backend, actions))
    with imu_simulation(firmware, actions) as sim:
        sim.step(7)
        old = sim.components['board']
        old_stream = old._streams['cosim']
        actions.clear()
        sim.reset()
        assert old_stream.sock.fileno() == -1
        assert sim.components['board'] is not old and sim.session.session_id == 2
        assert [client.retain_events for client in backend.clients] == [False, False]
        assert [a for a in actions if a[0] == 'imu receive'] == [('imu receive', 0, 71)]
        assert len([a for a in actions if a[0] == 'imu start']) == 1


@pytest.mark.parametrize('with_imu', [False, True])
def test_real_shared_cosim_socket_through_machine_commit_and_cleanup(
        firmware, backend, monkeypatch, with_imu):
    # This is a native-acquisition double, not firmware evidence. The actual
    # profile, endpoint, canonical framing, runtime and borrowed sockets run.
    from test_qemu_imu import Peer
    from sim.machines.qemu.cosim import QemuCosimEndpoint
    from sim.transports import step_codec as wire

    peers = []

    def endpoint(stream, **kwargs):
        peer_socket = next(peer for name, host, peer in backend.sockets
                           if name == 'cosim' and host is stream.sock)
        peer_socket.setblocking(True)

        def respond(peer, frame):
            if frame.header.kind == wire.RESET:
                peer.reset_ack(frame, capabilities=11, native=71)
            else:
                peer.ack(frame)
                if any(s.type == wire.STEP_SECTION_IMU_SAMPLE
                       for s in wire.decode_step_payload(frame.payload)):
                    peer.done(frame)

        peers.append(Peer(peer_socket, respond))
        return QemuCosimEndpoint(stream, **kwargs)

    monkeypatch.setattr(dm_profile, 'QemuCosimEndpoint', endpoint)
    sim = imu_simulation(firmware, [], adc=True) if with_imu else simulation(firmware, adc=True)
    try:
        sim.start()
        assert len(peers) == 1
        sim.schedule_input('board.adc1.ch0', {'raw': 12345}, at_ns=7)
        sim.schedule_input('board.adc1.ch1', {'voltage_uv': 1650000}, at_ns=7)
        sim.step(7)
        frames = peers[0].frames
        assert [(f.header.kind, f.header.step_id, f.header.t_sim_ns, f.header.dt_ns)
                for f in frames] == [(wire.RESET, 0, 0, 0), (wire.STEP, 1, 7, 7)]
        sections = wire.decode_step_payload(frames[1].payload)
        assert [s.type for s in sections] == ([1] if with_imu else []) + [3, 4]
        adc_events = [e for e in sim.events if e.kind == 'adc-input-accepted']
        assert [(e.source_port, dict(e.payload['input']), e.delivery_time_ns)
                for e in adc_events] == [('adc1.ch0', {'raw': 12345}, 7),
                                         ('adc1.ch1', {'voltage_uv': 1650000}, 7)]
        assert all(e.payload['step_id'] == 1 for e in adc_events)
        assert backend.clients[0].current == 78
        # Ordinary inputs can still be applied at this committed co-sim cut.
        sim.schedule_input('board.key', True)
        sim.step(1)
        assert any(name == 'qom-set' and arguments.get('property') == 'user-key'
                   for name, arguments in backend.commands)
        assert len(frames) == (3 if with_imu else 2)
    finally:
        for peer in peers:
            peer.stopping = True
        sim.close()
        for peer in peers:
            peer.thread.join(1)
            assert not peer.thread.is_alive() and not peer.errors
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)
    assert backend.calls[-2:] == ['QMP close', 'process close']


class FakeProfile:
    """A test profile using the same backend; it models no real board."""

    def __init__(self, machine_name='fixture-board'):
        self.actions = []
        self.close_error = None
        self.adapters = []
        self.machine_name = machine_name

    def describe(self, spec):
        assert not spec.options
        return NodeDescription((PortSpec('feed', 'bytes', direction='in',
            capabilities={'external_input'}), PortSpec('observation', 'bytes', direction='out')),
            {'machine', 'input'})

    def validate_input(self, spec, port, payload, *, connected_ports=()):
        if port != 'feed' or type(payload) is not bytes:
            raise ValueError('fake input requires bytes on feed')
        return payload

    def validate_connections(self, node, topology, descriptions):
        if topology.links:
            raise ValueError('fake profile has no connected ports')

    def create_adapter(self, spec, context, backend):
        profile = self

        class FakeAdapter:
            stream_types = {'data': StreamTransport}

            def __init__(self):
                self.completed = None

            def launch_command(self, qemu, sockets, *, qmp_path, gdb_port):
                from sim.machines.qemu.launch import native_qemu_command
                assert sockets == ['qmp', 'data']
                return native_qemu_command(qemu, sockets,
                    machine_arguments=('-machine', profile.machine_name), serial_slots=('data',),
                    qmp_path=qmp_path, gdb_port=gdb_port)

            def start(self):
                profile.actions.append(('start', backend.native_origin_ns))
                self.completed = (0, b'')

            def service(self, cut):
                profile.actions.append(('service', cut))

            def complete_cut(self, cut, target):
                stream = backend.streams['data']
                stream.flush()
                stream.read_once()
                self.completed = (cut, bytes(stream.buffer))
                stream.buffer.clear()
                context.emit('observation', 'fake.bytes', self.completed[1],
                    source_time_ns=target, mapped_time_ns=cut, quality='upper_bound')

            def input(self, port, data):
                assert port == 'feed'
                backend.streams['data'].send(data)

            def observe(self):
                return self.completed

            def close(self):
                profile.actions.append(('close',))
                if profile.close_error:
                    raise profile.close_error

        result = FakeAdapter()
        self.adapters.append(result)
        return result


def test_injected_profile_uses_same_native_target_lifecycle_without_dm_queries(backend, monkeypatch):
    profile = FakeProfile()
    monkeypatch.setattr(adapter, 'resolve_backend_root',
                        lambda _: pytest.fail('explicit QEMU must not require a checkout'))
    graph = Topology((NodeSpec('cpu', 'fixture.qemu'),), ())
    with Simulation(graph, {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')}) as sim:
        machine = sim.components['cpu']
        assert machine.observe() == (0, b'')
        assert machine.to_native(19) == 90 and machine.to_sim(90) == 19
        backend.sockets[0][2].sendall(b'opaque\0\xff')
        sim.schedule_input('cpu.feed', b'input\0')
        sim.step(19)
        assert backend.sockets[0][2].recv(4096) == b'input\0'
        assert machine.observe() == (19, b'opaque\0\xff')
        assert sim.events[-1].kind == 'fake.bytes'
        assert sim.events[-1].source_time_ns == 90
        command = backend.calls[0][1]
        assert command[command.index('-machine') + 1] == 'fixture-board'
        assert '-kernel' not in command
        assert all(name not in ('qom-list', 'qom-get', 'qom-set') for name, _ in backend.commands)
        previous = sim.components['cpu']
        sim.reset()
        assert previous is not sim.components['cpu']
        assert len(profile.adapters) == 2
    assert profile.actions.count(('close',)) == 2
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)
    assert backend.calls[-2:] == ['QMP close', 'process close']


@pytest.mark.parametrize('change', ['time', 'target', 'cancelled', 'running'])
def test_completion_callback_requires_fresh_exact_native_receipt_before_world_step(backend, change):
    from sim.api import StepResult

    profile, world_steps, world_closes = FakeProfile(), [], []

    class World:
        def step(self, step):
            world_steps.append(step.t_sim_ns)
            return StepResult('ok', step.t_sim_ns)

        def close(self):
            world_closes.append('closed')

    world = World()
    world_factory = SimpleNamespace(describe=lambda _: NodeDescription(capabilities={'step'}),
                                   create=lambda *_: world)

    def change_after_completion(event):
        if event.kind != 'fake.bytes' or event.mapped_time_ns != 38:
            return
        native = backend.clients[0]
        if change == 'time':
            native.current += 7
        elif change == 'target':
            native.target += 7
        elif change == 'cancelled':
            backend.state.external_change = True
        else:
            # Native reached is historical; an external cont need not change it.
            backend.state.read_status = lambda _: {'running': True, 'status': 'running'}

    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'), NodeSpec('world', 'fixture.world'))),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu'),
         'fixture.world': world_factory}, on_event=change_after_completion)
    sim.start()
    machine = sim.components['cpu']
    try:
        sim.step(19)
        assert world_steps == [19]
        with pytest.raises(RuntimeError) as failure:
            sim.step(19)
        assert sim.failure is failure.value
        assert sim.state == SimulationState.FAILED and sim.t_sim_ns == machine.t_sim_ns == 19
        assert machine.pending is not None and machine.pending.t_sim_ns == 38
        assert machine.observe()[0] == 38  # Completed adapter effects are not rolled back.
        assert world_steps == [19]
        before = list(backend.commands)
        with pytest.raises(RuntimeError, match='healthy'):
            sim.step(19)
        assert backend.commands == before
        assert sum(name == 'x-run-to-virtual-time' for name, _ in before) == 2
    finally:
        sim.close()
        sim.close()
    assert profile.actions.count(('close',)) == 1 and world_closes == ['closed']
    assert backend.calls.count('QMP close') == backend.calls.count('process close') == 1
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)


@pytest.mark.parametrize('profile_kind', ['generic', 'dm-mc02'])
def test_noncan_execution_observations_use_fresh_readonly_pairs(backend, firmware, profile_kind):
    if profile_kind == 'generic':
        profile = FakeProfile()
        sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),)),
            {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')})
    else:
        sim = simulation(firmware)
    pair = (('x-query-virtual-execution', None), ('query-status', None))
    with sim:
        backend.groups.clear()
        backend.commands.clear()
        sim.step(19)
        # Preflight, native completion, and post-callback receipt are fresh reads.
        assert backend.groups == [pair, pair, pair]
        assert sum(name == 'x-query-virtual-execution' for name, _ in backend.commands) == 3
        assert sum(name == 'query-status' for name, _ in backend.commands) == 3
        assert sim.t_sim_ns == 19 and sim.step_id == 1
        assert sum(name == 'x-run-to-virtual-time' for name, _ in backend.commands) == 1
        if profile_kind == 'generic':
            assert profile.adapters[0].observe() == (19, b'')
            assert all(name not in ('qom-get', 'qom-set', 'qom-list')
                       for name, _ in backend.commands)


def test_final_native_timestamp_rejects_bool_even_when_equal_to_target(backend):
    backend.state.origin = 0

    def invalid_timestamp(event):
        if event.kind == 'fake.bytes':
            backend.clients[0].current = True

    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),)),
        {'fixture.qemu': QemuFactory(profile=FakeProfile(), qemu='/explicit/qemu')},
        on_event=invalid_timestamp)
    with sim:
        machine = sim.components['cpu']
        with pytest.raises(ValueError, match='signed64') as failure:
            sim.step(1)
        assert sim.failure is failure.value and sim.state == SimulationState.FAILED
        assert sim.t_sim_ns == machine.t_sim_ns == 0 and machine.pending.t_sim_ns == 1
    assert backend.calls[-2:] == ['QMP close', 'process close']


@pytest.mark.parametrize('shape, error, match', [
    ('list', TypeError, 'plain tuple'),
    ('missing', ValueError, 'wrong arity'),
    ('extra', ValueError, 'wrong arity'),
])
def test_malformed_final_execution_pair_freezes_without_committing_or_replay(backend, monkeypatch,
                                                                          shape, error, match):
    profile = FakeProfile()

    def corrupt_pair(event):
        if event.kind != 'fake.bytes':
            return
        native = backend.clients[0]
        original = native.command_many

        def malformed(requests):
            values = original(requests)
            return {'list': list(values), 'missing': values[:1], 'extra': values + ({},)}[shape]

        monkeypatch.setattr(native, 'command_many', malformed)

    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),)),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')}, on_event=corrupt_pair)
    sim.start()
    machine = sim.components['cpu']
    try:
        with pytest.raises(error, match=match) as failure:
            sim.step(19)
        assert sim.failure is failure.value and sim.state == SimulationState.FAILED
        assert sim.t_sim_ns == machine.t_sim_ns == 0 and machine.pending is not None
        assert machine.observe() == (19, b'')
        before = list(backend.commands)
        with pytest.raises(RuntimeError, match='healthy'):
            sim.step(19)
        assert backend.commands == before
        assert sum(name == 'x-run-to-virtual-time' for name, _ in before) == 1
    finally:
        sim.close()
    assert profile.actions.count(('close',)) == 1
    assert backend.calls[-2:] == ['QMP close', 'process close']
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)


@pytest.mark.parametrize('operation, error', [
    ('x-query-virtual-execution', RuntimeError('native query rejected')),
    ('query-status', EOFError('native QMP peer closed')),
    ('x-query-virtual-execution', TimeoutError('native QMP stalled')),
])
def test_final_execution_query_failure_preserves_cause_and_closes_resources(backend, monkeypatch,
                                                                          operation, error):
    profile = FakeProfile()

    def fail_query(event):
        if event.kind != 'fake.bytes':
            return
        native = backend.clients[0]
        original = native.command

        def failed(name, arguments=None):
            if name == operation:
                raise error
            return original(name, arguments)

        monkeypatch.setattr(native, 'command', failed)

    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),)),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')}, on_event=fail_query)
    sim.start()
    machine = sim.components['cpu']
    try:
        with pytest.raises((RuntimeError, EOFError)) as failure:
            sim.step(19)
        if isinstance(error, TimeoutError):
            assert failure.value.__cause__ is error
        else:
            assert failure.value is error
        assert sim.failure is failure.value and sim.state == SimulationState.FAILED
        assert sim.t_sim_ns == machine.t_sim_ns == 0 and machine.pending is not None
        assert machine.observe() == (19, b'')
        before = list(backend.commands)
        with pytest.raises(RuntimeError, match='healthy'):
            sim.step(19)
        assert backend.commands == before
    finally:
        sim.close()
        sim.close()
    assert profile.actions.count(('close',)) == 1
    assert backend.calls.count('QMP close') == backend.calls.count('process close') == 1
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)


def test_profile_cleanup_error_still_closes_every_native_owner(backend):
    profile = FakeProfile()
    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),), ()),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')})
    sim.start()
    profile.close_error = OSError('profile cleanup failed')
    with pytest.raises(OSError) as caught:
        sim.close()
    assert caught.value is profile.close_error
    assert backend.calls[-2:] == ['QMP close', 'process close']
    assert backend.sockets[0][1].fileno() == -1


def test_two_profiles_keep_epochs_streams_and_cleanup_independent_and_peer_exit_freezes(backend):
    left, right = FakeProfile('fixture-left'), FakeProfile('fixture-right')
    backend.state.origins = (71, 901)
    sim = Simulation(Topology((NodeSpec('left', 'left.qemu'), NodeSpec('right', 'right.qemu'))),
        {'left.qemu': QemuFactory(profile=left, qemu='/explicit/qemu'),
         'right.qemu': QemuFactory(profile=right, qemu='/explicit/qemu')})
    sim.start()
    try:
        machines = sim.components
        assert machines['left'].to_native(5) == 76
        assert machines['right'].to_native(5) == 906
        assert machines['left'].connection_info['sockets'] != machines['right'].connection_info['sockets']
        sim.schedule_input('left.feed', b'left\0')
        sim.schedule_input('right.feed', b'right\xff')
        backend.sockets[0][2].sendall(b'reply-left')
        backend.sockets[1][2].sendall(b'reply-right')
        sim.run_until(5)
        assert [peer.recv(4096) for _, _, peer in backend.sockets] == [b'left\0', b'right\xff']
        assert [machines[name].observe() for name in ('left', 'right')] == [
            (5, b'reply-left'), (5, b'reply-right')]
        assert [client.target for client in backend.clients] == [76, 906]
        starts = [call[1] for call in backend.calls if isinstance(call, tuple) and call[0] == 'start']
        assert [command[command.index('-machine') + 1] for command in starts] == [
            'fixture-left', 'fixture-right']
        requests = sum(name == 'x-run-to-virtual-time' for name, _ in backend.commands)
        backend.state.processes[1].returncode = 0
        with pytest.raises(RuntimeError, match=r'QEMU exited \(0\)') as failure:
            sim.run_until(10)
        assert sim.failure is failure.value and sim.t_sim_ns == 5
        assert sum(name == 'x-run-to-virtual-time' for name, _ in backend.commands) == requests
        with pytest.raises(RuntimeError, match='healthy'):
            sim.step()
    finally:
        sim.close()
    assert left.actions.count(('close',)) == right.actions.count(('close',)) == 1
    assert backend.calls.count('QMP close') == backend.calls.count('process close') == 2
    assert all(host.fileno() == -1 for _, host, _ in backend.sockets)


def test_invalid_profile_adapter_is_closed_before_process_acquisition(backend):
    profile = FakeProfile()
    invalid = SimpleNamespace(close=lambda: profile.actions.append(('invalid close',)))
    profile.create_adapter = lambda *_: invalid
    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),), ()),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')})
    with pytest.raises(TypeError, match='profile adapter lacks'):
        sim.start()
    assert profile.actions == [('invalid close',)]
    assert backend.calls == backend.commands == backend.sockets == []


def test_advertised_profile_commit_requires_adapter_method_before_acquisition(backend):
    profile = FakeProfile()
    original = profile.describe
    profile.describe = lambda spec: NodeDescription(
        original(spec).ports, {'machine', 'input', 'input_commit'})
    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),), ()),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')})
    with pytest.raises(TypeError, match='advertised commit_inputs'):
        sim.start()
    assert profile.actions == [('close',)]
    assert backend.calls == backend.commands == backend.sockets == []


def test_missing_default_native_executable_is_backend_unavailable_before_io(backend, monkeypatch):
    monkeypatch.delenv('QEMU_SYSTEM_ARM', raising=False)
    monkeypatch.setattr(adapter, 'resolve_backend_root', lambda _: None)
    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),), ()),
        {'fixture.qemu': QemuFactory(profile=FakeProfile())})
    with pytest.raises(BackendUnavailable, match='supply --qemu'):
        sim.start()
    assert backend.calls == backend.commands == backend.sockets == []


@pytest.mark.parametrize('stream_types', [{'qmp': StreamTransport}, {'bad name': StreamTransport},
                                         {'data': None}])
def test_profile_stream_admission_closes_returned_adapter_before_io(backend, stream_types):
    profile = FakeProfile()
    original = profile.create_adapter

    def invalid(*args):
        result = original(*args)
        result.stream_types = stream_types
        return result

    profile.create_adapter = invalid
    sim = Simulation(Topology((NodeSpec('cpu', 'fixture.qemu'),), ()),
        {'fixture.qemu': QemuFactory(profile=profile, qemu='/explicit/qemu')})
    with pytest.raises(TypeError, match='short names'):
        sim.start()
    assert profile.actions == [('close',)]
    assert backend.calls == backend.commands == backend.sockets == []
