"""Referee-domain admission and real Simulation/net.uart direct consumers."""

from dataclasses import asdict
import importlib.abc
from pathlib import Path
import subprocess
import sys

import pytest

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Session, Topology
from sim.devices.referee import Buff, GameStatus, PowerHeat, RefereeState, RobotStatus, decode_frame
from sim.devices.referee.config import parse_referee_configuration, parse_referee_events
from sim.devices.referee.factory import RefereeFactory
from sim.fabric.uart import UartNetFactory
from sim.runtime import Simulation, SimulationState


INITIAL = RefereeState(GameStatus(1, 0, 0, 0x0102030405060708),
                       RobotStatus(3, 2, 100, 200, 3, 100, 80, 7),
                       PowerHeat(40, 10, 20), Buff(0, 0, 0, 0, 0, 0x80))


def options(**changes):
    result = {"initial": asdict(INITIAL), "durations": {"countdown_s": 3, "battle_s": 4},
              "period": "1ms"}
    result.update(changes)
    return result


def decode_batch(wire):
    result = []
    while wire:
        end = int.from_bytes(wire[1:3], "little") + 9
        result.append(decode_frame(wire[:end]))
        wire = wire[end:]
    assert len(result) == 4
    return result


class Receiver:
    def __init__(self, context, *, reply=False):
        self.received = []
        self.reply = reply
        self.connection = context.attach("uart", self)
        self.closed = False

    def receive_uart(self, data):
        self.received.append(data)
        if self.reply:
            self.connection.send(b"unsupported uplink bytes")

    def close(self):
        self.closed = True


class ReceiverFactory:
    def __init__(self, *, reply=False):
        self.reply = reply

    def describe(self, spec):
        return NodeDescription((PortSpec("uart", "uart", min_connections=1),))

    def create(self, spec, context):
        self.instance = Receiver(context, reply=self.reply)
        return self.instance


def simulation(*, config=None, wire_options=None, receiver=None, **runtime):
    receiver = receiver or ReceiverFactory()
    topology = Topology((NodeSpec("referee", "device.referee", config or options()),
                         NodeSpec("wire", "net.uart", wire_options or {}),
                         NodeSpec("guest", "receiver")),
                        (LinkSpec("referee.uart", "wire.uart"),
                         LinkSpec("guest.uart", "wire.uart")))
    sim = Simulation(topology, {"device.referee": RefereeFactory(),
                               "net.uart": UartNetFactory(), "receiver": receiver}, **runtime)
    return sim, receiver


def test_pure_admission_imports_no_optional_backend_or_checksum(tmp_path):
    script = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'crccheck','qemu','mujoco','numpy','tools'}:
            raise AssertionError('optional import: ' + fullname)
sys.meta_path.insert(0, Guard())
from sim.api import NodeSpec
from sim.devices.referee.factory import RefereeFactory
spec = NodeSpec('referee', 'device.referee', CONFIG)
assert 'dispatch' in RefereeFactory.describe(spec).capabilities
assert RefereeFactory.validate_input(spec, 'enable', True, connected_ports=()) is True
'''
    script = script.replace("CONFIG", repr(options(events=[{"offset_ns": 0, "hit": {"damage_hp": 5}}])))
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_missing_checksum_is_blocked_when_encoding_not_during_validation(monkeypatch):
    class Missing(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] == "crccheck":
                raise ModuleNotFoundError("missing optional referee codec", name="crccheck")
    from sim.api import BackendUnavailable
    from sim.devices.referee import encode_message
    for key in tuple(sys.modules):
        if key.split(".")[0] == "crccheck":
            monkeypatch.delitem(sys.modules, key)
    monkeypatch.setattr(sys, "meta_path", [Missing(), *sys.meta_path])
    assert RefereeFactory.describe(NodeSpec("referee", "device.referee", options()))
    with pytest.raises(BackendUnavailable, match="referee extra"):
        encode_message(INITIAL.game)


@pytest.mark.parametrize("change", [
    {"period": "999us"}, {"period": "60001ms"}, {"period": True}, {"unknown": 0},
    {"events": [{"offset_ns": 1, "hit": {"damage_hp": 2}},
                {"offset_ns": 0, "hit": {"damage_hp": 3}}]},
    {"events": [{"offset_ns": 0, "hit": {"damage_hp": 2}, "shot": {"heat_17mm": 0, "heat_42mm": 0}}]},
    {"durations": {"countdown_s": 0, "battle_s": 4}},
    {"cooling_42mm_per_s": True},
])
def test_invalid_configuration_rejected_before_create(change):
    with pytest.raises((ValueError, TypeError)):
        RefereeFactory.describe(NodeSpec("referee", "device.referee", options(**change)))


def test_configuration_owner_preserves_equal_time_event_order():
    config = options(events=[{"offset_ns": 0, "hit": {"damage_hp": 5}},
                             {"offset_ns": 0, "robot": asdict(INITIAL.robot)},
                             {"offset_ns": 0, "hit": {"damage_hp": 7}}])
    config.pop("period")
    admitted = parse_referee_configuration(config)
    assert [type(event.value).__name__ for event in admitted["events"]] == ["Hit", "RobotStatus", "Hit"]
    assert parse_referee_events(()) == ()


@pytest.mark.parametrize("payload", [False, 1, None, "true", {}])
def test_enable_admission_is_pure_true_only(payload):
    spec = NodeSpec("referee", "device.referee", options())
    with pytest.raises(ValueError, match="requires true"):
        RefereeFactory.validate_input(spec, "enable", payload, connected_ports=("uart",))


def test_reset_relative_engine_first_enable_cut_absolute_period_and_no_replay():
    config = options(events=[{"offset_ns": 500_000, "hit": {"damage_hp": 30}}])
    sim, receiver = simulation(config=config, wire_options={"delay_ns": 250_000},
                               initial_session=Session(7, 100), quantum_ns=10_000_000)
    with sim:
        sim.run_until(1_000_100)
        assert not receiver.instance.received
        assert sim._components["referee"].get_state().robot.current_hp == 70
        sim.schedule_input("referee.enable", True, at_ns=1_250_100)
        sim.schedule_input("referee.enable", True, at_ns=1_800_100)
        sim.run_until(3_500_100)
        batches = [event for event in sim.events if event.kind == "referee.batch.admitted"]
        assert [event.delivery_time_ns for event in batches] == [1_250_100, 2_250_100, 3_250_100]
        assert [event.payload["first_sequence"] for event in batches] == [0, 4, 8]
        deliveries = [event for event in sim.events if event.kind == "net.uart.delivery"]
        assert [event.delivery_time_ns for event in deliveries] == [1_500_100, 2_500_100, 3_500_100]
        assert len(receiver.instance.received) == 3
        for batch, wire in zip(batches, receiver.instance.received):
            assert batch.payload["data"] == wire and len(wire) == 82
            decoded = decode_batch(wire)
            assert [frame.command_id for frame in decoded] == [0x0001, 0x0201, 0x0202, 0x0204]
            assert decoded[1].payload.hex() == "03024600c80003006400500007"
            assert batch.source_time_ns == batch.mapped_time_ns == batch.delivery_time_ns
        before = len(receiver.instance.received)
        sim.run_until(sim.t_sim_ns)
        assert len(receiver.instance.received) == before
    assert receiver.instance.closed


def test_reverse_uart_is_explicitly_ignored_without_response_loop():
    receiver = ReceiverFactory(reply=True)
    sim, _ = simulation(receiver=receiver)
    with sim:
        sim.schedule_input("referee.enable", True)
        sim.run_until(0)
        assert len(receiver.instance.received) == 1
        assert sim._components["referee"].publications == 1
        assert len([event for event in sim.events if event.kind == "net.uart.delivery"]) == 2
        assert not sim._components["wire"].pending


def test_net_capacity_failure_freezes_device_no_sequence_commit_or_retry():
    sim, receiver = simulation(wire_options={"max_pending_bytes": 81})
    sim.start()
    device = sim._components["referee"]
    sim.schedule_input("referee.enable", True)
    with pytest.raises(BufferError) as raised:
        sim.run_until(0)
    assert sim.state == SimulationState.FAILED and not receiver.instance.received
    assert device.next_sequence == device.publications == 0
    with pytest.raises(BufferError) as repeated:
        device.dispatch(0, 1)
    assert repeated.value is raised.value
    assert not sim._components["wire"].pending
    sim.close()


def test_metadata_failure_keeps_accepted_bytes_and_sequence_without_retransmit():
    failure = OSError("observation sink failed")
    def observe(event):
        if event.kind == "referee.batch.admitted":
            raise failure
    sim, receiver = simulation(on_event=observe)
    sim.start()
    device, wire = sim._components["referee"], sim._components["wire"]
    sim.schedule_input("referee.enable", True)
    with pytest.raises(OSError) as raised:
        sim.run_until(0)
    assert raised.value is failure and device.next_sequence == 4 and device.publications == 1
    assert wire.pending == 1 and wire.pending_bytes == 82 and not receiver.instance.received
    with pytest.raises(OSError) as repeated:
        device.dispatch(0, 1)
    assert repeated.value is failure and wire.pending == 1
    sim.close()


def test_reset_recreates_disabled_owner_and_restores_script_and_sequence():
    config = options(events=[{"offset_ns": 0, "hit": {"damage_hp": 7}}])
    sim, receiver = simulation(config=config)
    with sim:
        sim.schedule_input("referee.enable", True)
        sim.run_until(0)
        previous = sim._components["referee"]
        sim.reset()
        current = sim._components["referee"]
        assert current is not previous and current.next_sequence == current.publications == 0
        assert current.next_due_ns is None and current.get_state().robot.current_hp == 93
        sim.run_until(1_000_000)
        assert not receiver.instance.received
        sim.schedule_input("referee.enable", True)
        sim.run_until(1_000_000)
        assert current.publications == 1 and decode_batch(receiver.instance.received[0])[1].payload[2:4] == b"]\x00"


def test_next_deadline_overflow_rejects_before_batch_io():
    maximum = (1 << 64) - 1
    sim, receiver = simulation(initial_session=Session(1, maximum))
    with sim:
        sim.schedule_input("referee.enable", True)
        with pytest.raises(ValueError, match="unsigned 64"):
            sim.run_until(maximum)
        assert not receiver.instance.received and sim._components["referee"].next_sequence == 0


def test_caught_callback_reentry_still_freezes_after_single_batch_admission():
    device = None
    def observe(event):
        if event.kind == "net.uart.sent":
            with pytest.raises(RuntimeError, match="reentered"):
                device.input("enable", True)
    sim, receiver = simulation(on_event=observe)
    sim.start()
    device = sim._components["referee"]
    sim.schedule_input("referee.enable", True)
    with pytest.raises(RuntimeError, match="reentered"):
        sim.run_until(0)
    assert sim.state == SimulationState.FAILED
    assert device.next_sequence == 4 and sim._components["wire"].pending == 1
    assert not receiver.instance.received
    sim.close()
