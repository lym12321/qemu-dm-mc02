"""C620 wire oracle and simple-plant/direct Fabric regression gates.

Literal vectors follow the DJI C620 V1.01 publisher PDF pp14–17/31–33
(SHA256 f422c7a6…f71b), not production encoders. Physics values below are
independent hand calculations for the declared approximation, not hardware.
"""

import math
from dataclasses import FrozenInstanceError, replace

import pytest

from sim.api import Session, StepContext
from sim.devices.dji import M3508, M3508Device, decode_current_command, encode_feedback
from sim.fabric import CanBus, CanFrame
from sim.fabric.frame import WIRE_EXTENDED, WIRE_FD, WIRE_RTR
from sim.physics import Rotor


class Host:
    def __init__(self, bus):
        self.frames = []
        self.bus = bus
        bus.attach(self, "host")

    def receive(self, frame):
        self.frames.append(frame)

    def send(self, can_id, data, timestamp_ns=0, **kwargs):
        self.bus.send(self, CanFrame(can_id, data=data,
                                    timestamp_ns=timestamp_ns, **kwargs))


def advance(device, dt_ns, step_id=1):
    return device.step(StepContext(1, step_id,
                                   device.t_sim_ns + dt_ns, dt_ns))


def test_literal_command_slots_groups_signed_and_limits():
    # DJI table: four BE signed slots, +/-16384 maps +/-20 A.
    payload = bytes.fromhex("1000 f000 4000 c000")
    first = CanFrame(0x200, data=payload)
    second = CanFrame(0x1FF, data=payload)
    assert [decode_current_command(first, i) for i in range(1, 5)] == [
        4096, -4096, 16384, -16384]
    assert [decode_current_command(second, i) for i in range(5, 9)] == [
        4096, -4096, 16384, -16384]
    assert decode_current_command(second, 1) is None
    assert decode_current_command(first, 5) is None
    assert decode_current_command(CanFrame(0x201, data=payload), 1) is None


def test_feedback_explicit_fields_have_literal_wire_bytes():
    frame = encode_feedback(5, encoder=4096, speed_rpm=-1200,
                            current_raw=-4096, temperature_c=25, timestamp_ns=123)
    assert (frame.can_id, frame.flags, frame.dlc, frame.timestamp_ns) == (
        0x205, 0, 8, 123)
    assert frame.data == bytes.fromhex("1000 fb50 f000 19 00")
    assert encode_feedback(8, encoder=8191, speed_rpm=-32768,
                           current_raw=32767, temperature_c=255).data == \
        bytes.fromhex("1fff 8000 7fff ff 00")


@pytest.mark.parametrize("kwargs", [
    {"encoder": -1}, {"encoder": 8192}, {"encoder": True},
    {"speed_rpm": -32769}, {"speed_rpm": 32768},
    {"current_raw": 32768}, {"temperature_c": 256},
])
def test_feedback_rejects_unrepresentable_fields(kwargs):
    fields = dict(encoder=0, speed_rpm=0, current_raw=0)
    fields.update(kwargs)
    with pytest.raises(ValueError):
        encode_feedback(1, **fields)


def test_malformed_command_rejects_whole_group_and_retains_current_and_expiry():
    bus = CanBus()
    device = M3508(bus)
    device.reset(Session(1))
    device.receive(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000")))
    assert (device.current_raw, device.current_amp) == (4096, 5.0)
    advance(device, 40_000_000)
    bad_frames = [
        CanFrame(0x200, flags=WIRE_EXTENDED, data=bytes(8), timestamp_ns=40_000_000),
        CanFrame(0x200, flags=WIRE_RTR, dlc=8, timestamp_ns=40_000_000),
        CanFrame(0x200, flags=WIRE_FD, data=bytes(8), timestamp_ns=40_000_000),
        CanFrame(0x200, data=bytes(7), timestamp_ns=40_000_000),
        CanFrame(0x200, dlc=8, data=bytes(7), timestamp_ns=40_000_000),
        CanFrame(0x200, data=bytes.fromhex("4001 0000 0000 0000"),
                 timestamp_ns=40_000_000),
        # An invalid peer slot rejects the entire group, including our valid slot.
        CanFrame(0x200, data=bytes.fromhex("0000 bfff 0000 0000"),
                 timestamp_ns=40_000_000),
    ]
    for frame in bad_frames:
        device.receive(frame)
        assert device.current_raw == 4096
    assert device.rejected_commands == len(bad_frames)
    assert "-16384..16384" in device.last_rejection
    advance(device, 60_000_000, 2)
    # Rejected traffic at 40ms must not extend the initial 100ms expiry.
    assert device.current_raw == 0
    assert device.feedback().data[4:6] == bytes(2)


def test_rotor_reuses_declared_velocity_then_position_integration():
    rotor = Rotor(initial_position=2.0, initial_velocity=1.0)
    rotor.step(0.01, 0.4)
    # (.4-.08*1)/.02=16 rad/s²; v=1.16 then q=2.0116.
    assert rotor.velocity == pytest.approx(1.16)
    assert rotor.position == pytest.approx(2.0116)
    rotor.reset()
    assert (rotor.position, rotor.velocity) == (2.0, 1.0)


def test_negative_rotor_angle_wrap_and_rotor_rpm_without_gearbox_conversion():
    bus = CanBus()
    device = M3508(bus, 5, rotor=Rotor(initial_position=-math.pi / 2,
                                      initial_velocity=-40 * math.pi))
    device.reset(Session(1))
    # -1/4 revolution wraps to encoder6144; -40π rad/s is -1200 rotor RPM.
    assert device.feedback().data == bytes.fromhex("1800 fb50 0000 19 00")
    forward = M3508(bus, 1, rotor=Rotor(initial_position=2 * math.tau + math.pi,
                                       initial_velocity=40 * math.pi))
    forward.reset(Session(1))
    assert forward.feedback().data == bytes.fromhex("1000 04b0 0000 19 00")


def test_command_timeout_splits_large_step_and_continues_latest_feedback():
    bus = CanBus()
    host = Host(bus)
    device = M3508(bus)
    device.reset(Session(1))
    host.send(0x200, bytes.fromhex("4000 0000 0000 0000"))
    bus.step(0)
    result = advance(device, 150_000_000)
    # Drive .4Nm for .1s: v=2,q=.2. Coast .05s: v=1.6,q=.28.
    assert device.rotor.velocity == pytest.approx(1.6)
    assert device.rotor.position == pytest.approx(0.28)
    assert result.reached_time_ns == 150_000_000
    assert device.current_raw == 0
    assert bus.pending == 1  # Not150 missed feedback samples.
    bus.step(150_000_000)
    assert len(host.frames) == 1
    assert host.frames[-1].data == bytes.fromhex("016d 000f 0000 19 00")
    advance(device, 1_000_000, 2)
    bus.step(151_000_000)
    assert len(host.frames) == 2
    assert host.frames[-1].timestamp_ns == 151_000_000
    assert host.frames[-1].data[4:6] == bytes(2)


def test_feedback_period_keeps_phase_without_catchup():
    bus = CanBus()
    device = M3508(bus)
    device.reset(Session(1))
    advance(device, 500_000)
    assert bus.pending == 0
    advance(device, 3_000_000, 2)
    assert bus.pending == 1
    bus.step(3_500_000)
    advance(device, 400_000, 3)
    assert bus.pending == 0
    advance(device, 100_000, 4)
    assert bus.pending == 1


def test_two_groups_fabric_targeted_feedback_drop_and_recovery():
    events = []
    dropping = True
    host = None

    def drop(source, destination, frame):
        return dropping and destination is host and frame.can_id == 0x201

    bus = CanBus(drop=drop, log=events.append)
    host = Host(bus)
    first, fifth = M3508(bus, 1), M3508(bus, 5)
    for device in (first, fifth):
        device.reset(Session(1))
    host.send(0x200, bytes.fromhex("1000 0000 0000 0000"))
    host.send(0x1FF, bytes.fromhex("f000 0000 0000 0000"))
    bus.step(0)
    for device in (first, fifth):
        advance(device, 1_000_000)
    bus.step(1_000_000)
    assert first.rotor.velocity == pytest.approx(0.005)
    assert fifth.rotor.velocity == pytest.approx(-0.005)
    assert [(frame.can_id, frame.data) for frame in host.frames] == [
        (0x205, bytes.fromhex("1fff 0000 f000 19 00"))]
    assert any(e.destination == "host" and e.frame.can_id == 0x201 and e.dropped
               for e in events)
    dropping = False
    for device in (first, fifth):
        advance(device, 1_000_000, 2)
    bus.step(2_000_000)
    assert [frame.can_id for frame in host.frames[-2:]] == [0x201, 0x205]
    assert host.frames[-2].data[4:6] == bytes.fromhex("1000")


def test_zero_command_coasts_and_new_command_after_timeout_recovers():
    bus = CanBus()
    device = M3508(bus)
    device.reset(Session(1))
    device.receive(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000")))
    advance(device, 1_000_000)
    device.receive(CanFrame(0x200, data=bytes(8), timestamp_ns=1_000_000))
    advance(device, 1_000_000, 2)
    assert device.rotor.velocity == pytest.approx(0.00498)
    assert device.current_raw == 0
    advance(device, 101_000_000, 3)
    device.receive(CanFrame(0x200, data=bytes.fromhex("f000 0000 0000 0000"),
                            timestamp_ns=103_000_000))
    assert device.current_amp == -5.0
    advance(device, 1_000_000, 4)
    assert device.rotor.velocity < 0


def test_participant_admission_reset_and_close_preserve_borrowed_bus():
    bus = CanBus()
    host = Host(bus)
    device = M3508(bus)
    with pytest.raises(RuntimeError, match="successful reset"):
        device.step(StepContext(1, 1, 1_000_000, 1_000_000))
    device.reset(Session(1))
    with pytest.raises(ValueError, match="current device time"):
        device.receive(CanFrame(0x200, data=bytes(8), timestamp_ns=1))
    with pytest.raises(ValueError, match="exact target"):
        device.step(StepContext(2, 1, 1_000_000, 1_000_000))
    assert (device.t_sim_ns, device.current_raw, bus.pending) == (0, 0, 0)
    device.receive(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000")))
    advance(device, 1_000_000)
    bus.reset(10)
    device.reset(Session(2, 10))
    assert (device.t_sim_ns, device.current_raw, device.rotor.position) == (10, 0, 0)
    device.close()
    device.close()
    with pytest.raises(RuntimeError):
        device.reset(Session(3))
    # Closing the participant must not close/reset its borrowed Fabric.
    host.send(0x123, b"live", timestamp_ns=10)
    assert bus.pending == 1


@pytest.mark.parametrize("kwargs", [
    {"motor_id": 0}, {"motor_id": 9}, {"motor_id": True},
    {"torque_per_amp": float("nan")}, {"torque_per_amp": -1},
    {"command_timeout_ns": 0}, {"command_timeout_ns": True},
    {"feedback_period_ns": 1.5}, {"temperature_c": 256}, {"rotor": object()},
])
def test_invalid_device_configuration_does_not_attach(kwargs):
    bus = CanBus()
    with pytest.raises((ValueError, TypeError)):
        M3508(bus, **kwargs)
    # Valid default identity still attaches after rejection.
    M3508(bus)


@pytest.mark.parametrize("kwargs", [
    {"inertia": 0}, {"damping": -1}, {"initial_velocity": float("inf")},
])
def test_invalid_rotor_configuration(kwargs):
    with pytest.raises(ValueError):
        Rotor(**kwargs)


def test_protocol_prepare_has_no_physics_or_feedback_and_complete_uses_literal_sample():
    bus = CanBus()
    host = Host(bus)
    device = M3508Device(bus)
    device.reset(Session(1), position_rad=2, velocity_rad_s=3)
    device.receive(CanFrame(0x200, data=bytes.fromhex("f000 0000 0000 0000")))
    ctx = StepContext(1, 1, 1_000_000, 1_000_000)
    plan = device.prepare_step(ctx)
    assert plan.context is ctx
    assert plan.active_until_ns == 1_000_000
    assert plan.effort_nm == pytest.approx(-.1)
    assert device.get_rotor_state() == (2, 3)
    assert device.t_sim_ns == 0 and bus.pending == 0 and host.frames == []
    assert not hasattr(device, "rotor")
    with pytest.raises(FrozenInstanceError):
        plan.effort_nm = 0
    result = device.complete_step(plan, -math.pi / 2, -40 * math.pi)
    assert result.status == "ok" and result.reached_time_ns == 1_000_000
    assert device.get_rotor_state() == (-math.pi / 2, -40 * math.pi)
    bus.step(1_000_000)
    assert len(host.frames) == 1
    frame = host.frames[0]
    # Independent vendor fields: encoder6144, rotorRPM-1200, commandRaw-4096.
    assert (frame.can_id, frame.timestamp_ns, frame.data) == (
        0x201, 1_000_000, bytes.fromhex("1800 fb50 f000 19 00"))


@pytest.mark.parametrize("raw,target,active,effort", [
    ("4000", 100_000_000, 100_000_000, .4),
    ("4000", 150_000_000, 100_000_000, .4),
    ("0000", 150_000_000, 0, 0),
])
def test_protocol_timeout_and_zero_current_plan_boundaries(raw, target, active, effort):
    bus = CanBus()
    device = M3508Device(bus)
    device.reset(Session(1), position_rad=7, velocity_rad_s=5)
    device.receive(CanFrame(0x200, data=bytes.fromhex(raw + " 0000 0000 0000")))
    plan = device.prepare_step(StepContext(1, 1, target, target))
    assert (plan.active_until_ns, plan.effort_nm) == (active, effort)
    assert device.t_sim_ns == 0 and device.get_rotor_state() == (7, 5)
    assert bus.pending == 0
    device.complete_step(plan, 8, 6)
    assert device.current_raw == 0
    assert bus.pending == 1  # Coalesces all missed feedback deadlines.
    next_plan = device.prepare_step(StepContext(1, 2, target + 1_000_000, 1_000_000))
    assert next_plan.active_until_ns == target and next_plan.effort_nm == 0


def test_protocol_pending_command_and_plan_identity_do_not_change_completed_state():
    bus = CanBus()
    first, fifth = M3508Device(bus, 1), M3508Device(bus, 5)
    first.reset(Session(1))
    fifth.reset(Session(1))
    first.receive(CanFrame(0x200, data=bytes.fromhex("1000 0000 0000 0000")))
    ctx = StepContext(1, 1, 1_000_000, 1_000_000)
    plan = first.prepare_step(ctx)
    other_plan = fifth.prepare_step(ctx)
    with pytest.raises(RuntimeError):
        first.prepare_step(ctx)
    with pytest.raises(RuntimeError):
        first.receive(CanFrame(0x200, data=bytes(8)))
    assert first.current_raw == 4096
    for rejected in (other_plan, replace(plan)):
        with pytest.raises((ValueError, RuntimeError)):
            first.complete_step(rejected, 9, 8)
    assert first.t_sim_ns == 0 and first.get_rotor_state() == (0, 0)
    assert bus.pending == 0
    first.complete_step(plan, 1, 2)
    assert first.get_rotor_state() == (1, 2)
    with pytest.raises((ValueError, RuntimeError)):
        first.complete_step(plan, 3, 4)


def test_protocol_bad_sample_is_rejected_before_commit_and_original_plan_remains_live():
    bus = CanBus()
    device = M3508Device(bus)
    device.reset(Session(1), position_rad=1, velocity_rad_s=2)
    plan = device.prepare_step(StepContext(1, 1, 1_000_000, 1_000_000))
    for position, velocity in ((math.nan, 0), (0, math.inf), (True, 0), (0, "1")):
        with pytest.raises((ValueError, TypeError)):
            device.complete_step(plan, position, velocity)
        assert device.t_sim_ns == 0 and device.get_rotor_state() == (1, 2)
        assert bus.pending == 0
    device.complete_step(plan, .1, .2)
    assert device.t_sim_ns == 1_000_000 and device.get_rotor_state() == (.1, .2)


def test_protocol_reset_invalidates_old_plan_and_enforces_new_session_exact_context():
    bus = CanBus()
    device = M3508Device(bus)
    with pytest.raises(RuntimeError):
        device.prepare_step(StepContext(1, 1, 1_000_000, 1_000_000))
    device.reset(Session(1))
    stale = device.prepare_step(StepContext(1, 1, 1_000_000, 1_000_000))
    device.reset(Session(2, 10), position_rad=3, velocity_rad_s=4)
    with pytest.raises((ValueError, RuntimeError)):
        device.complete_step(stale, 9, 9)
    for ctx in (StepContext(1, 1, 1_000_010, 1_000_000),
                StepContext(2, 2, 1_000_010, 1_000_000),
                StepContext(2, 1, 1_000_011, 1_000_000)):
        with pytest.raises(ValueError):
            device.prepare_step(ctx)
    assert device.get_rotor_state() == (3, 4) and device.t_sim_ns == 10
    plan = device.prepare_step(StepContext(2, 1, 1_000_010, 1_000_000))
    device.complete_step(plan, 5, 6)
    assert device.get_rotor_state() == (5, 6)


def test_protocol_completed_feedback_coalesces_but_keeps_original_phase():
    bus = CanBus()
    host = Host(bus)
    device = M3508Device(bus)
    device.reset(Session(1))
    for step_id, target, dt, expected_pending in (
            (1, 500_000, 500_000, 0),
            (2, 3_500_000, 3_000_000, 1),
            (3, 3_900_000, 400_000, 0),
            (4, 4_000_000, 100_000, 1)):
        plan = device.prepare_step(StepContext(1, step_id, target, dt))
        device.complete_step(plan, 0, 0)
        assert bus.pending == expected_pending
        bus.step(target)
    assert [frame.timestamp_ns for frame in host.frames] == [3_500_000, 4_000_000]


def test_protocol_emit_rejection_preserves_committed_sample_without_plan_replay():
    bus = CanBus(max_pending=1)
    host = Host(bus)
    device = M3508Device(bus)
    device.reset(Session(1))
    plan = device.prepare_step(StepContext(1, 1, 1_000_000, 1_000_000))
    host.send(0x123, b"occupied", timestamp_ns=10_000_000)
    with pytest.raises(BufferError):
        device.complete_step(plan, 3, 4)
    assert device.t_sim_ns == 1_000_000 and device.get_rotor_state() == (3, 4)
    with pytest.raises((ValueError, RuntimeError)):
        device.complete_step(plan, 5, 6)
    device.reset(Session(2))
    assert bus.pending == 1  # Reset never consumes another owner's queued wire.


def test_protocol_close_cancels_pending_and_preserves_borrowed_bus():
    bus = CanBus()
    host = Host(bus)
    device = M3508Device(bus)
    device.reset(Session(1))
    plan = device.prepare_step(StepContext(1, 1, 1_000_000, 1_000_000))
    device.close()
    device.close()
    with pytest.raises(RuntimeError):
        device.complete_step(plan, 1, 2)
    with pytest.raises(RuntimeError):
        device.prepare_step(StepContext(1, 1, 1_000_000, 1_000_000))
    with pytest.raises(RuntimeError):
        device.reset(Session(2))
    host.send(0x123, b"live")
    assert bus.pending == 1


def test_legacy_get_rotor_state_and_feedback_follow_live_external_rotor_changes():
    device = M3508(CanBus())
    device.reset(Session(1))
    device.rotor.position = -math.pi / 2
    device.rotor.velocity = -40 * math.pi
    assert device.get_rotor_state() == (-math.pi / 2, -40 * math.pi)
    assert device.feedback().data == bytes.fromhex("1800 fb50 0000 19 00")


def test_zero_torque_coefficient_keeps_timeout_damping_segments():
    device = M3508(CanBus(), torque_per_amp=0,
                   rotor=Rotor(initial_velocity=1))
    device.reset(Session(1))
    device.receive(CanFrame(0x200, data=bytes.fromhex("4000 0000 0000 0000")))
    advance(device, 150_000_000)
    # Original two half-implicit intervals: v=.6,q=.06 at100ms;
    # then v=.48,q=.084 at150ms. One150ms coast gives different state.
    assert device.get_rotor_state() == pytest.approx((.084, .48))


def test_protocol_finite_extreme_velocity_saturates_wire_before_rpm_overflow():
    device = M3508Device(CanBus())
    for velocity, literal in ((1e308, "0000 7fff 0000 19 00"),
                              (-1e308, "0000 8000 0000 19 00")):
        device.reset(Session(1), velocity_rad_s=velocity)
        assert device.feedback().data == bytes.fromhex(literal)
    with pytest.raises(ValueError, match="finite.*torque"):
        M3508Device(CanBus(), torque_per_amp=1e308)
