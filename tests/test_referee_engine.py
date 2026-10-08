"""Explicit script/time contracts; numeric expectations do not call engine helpers."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.core import Coordinator, Session, StepContext, StepResult
from sim.devices.referee import (
    Buff, GameStatus, Hit, MatchDurations, PowerHeat, RefereeEngine, RefereeState,
    RobotStatus, ScriptEvent, Shot, decode_frame, encode_message,
    validate_referee_configuration,
)

NS = 1_000_000_000
INITIAL = RefereeState(GameStatus(1, 0, 0, 0x0102030405060708),
                       RobotStatus(3, 2, 100, 200, 3, 100, 80, 7),
                       PowerHeat(40, 10, 20), Buff(0, 0, 0, 0, 0, 0x80))
DURATIONS = MatchDurations(3, 4)


def engine(initial=INITIAL, **options):
    return RefereeEngine(initial, durations=options.pop("durations", DURATIONS), **options)


def advance(consumer, target, step_id, session_id=7):
    return consumer.step(StepContext(session_id, step_id, target, target - consumer.t_sim_ns))


@pytest.mark.parametrize("admit", [RefereeEngine, validate_referee_configuration],
                         ids=["engine", "pure"])
@pytest.mark.parametrize("initial,options", [
    (object(), {}),
    (replace(INITIAL, robot=replace(INITIAL.robot, maximum_hp=99)), {}),
    (replace(INITIAL, game=GameStatus(1, 2, 0, 0)), {}),
    (replace(INITIAL, power_heat=PowerHeat(True, 0, 0)), {}),
    (INITIAL, {"durations": MatchDurations(0, 1)}),
    (INITIAL, {"durations": MatchDurations(1, 65536)}),
    (INITIAL, {"durations": MatchDurations(True, 1)}),
    (INITIAL, {"cooling_42mm_per_s": -1}),
    (INITIAL, {"cooling_42mm_per_s": True}),
    (INITIAL, {"events": []}),
    (INITIAL, {"events": (ScriptEvent(0, Hit(0)),) * 1025}),
    (INITIAL, {"events": (ScriptEvent(1, Hit(0)), ScriptEvent(0, Hit(0)))}),
    (INITIAL, {"events": (ScriptEvent(True, Hit(0)),)}),
    (INITIAL, {"events": (ScriptEvent(1 << 64, Hit(0)),)}),
    (INITIAL, {"events": (ScriptEvent(0, object()),)}),
    (INITIAL, {"events": (ScriptEvent(0, Hit(True)),)}),
    (INITIAL, {"events": (ScriptEvent(0, Shot(-1, 0)),)}),
    (INITIAL, {"events": (ScriptEvent(0, Shot(0, 65536)),)}),
    (INITIAL, {"events": (ScriptEvent(0, GameStatus(1, 4, 0, 0)),)}),
    (INITIAL, {"events": (ScriptEvent(0, replace(INITIAL.robot, current_hp=201)),)}),
])
def test_configuration_admission_is_pure_and_reuses_wire_constraints(monkeypatch, admit, initial, options):
    def forbidden(*args, **kwargs):
        pytest.fail("referee configuration performed external IO")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises((ValueError, TypeError)):
        admit(initial, **{"durations": DURATIONS, **options})


def test_public_configuration_admission_needs_no_owner_or_runtime(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure configuration admission constructed or executed an owner")
    for method in ("__init__", "reset", "step", "get_state", "close"):
        monkeypatch.setattr(RefereeEngine, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # Equal-time entries are admitted in tuple order, without applying the hit.
    script = (ScriptEvent(0, Hit(20)), ScriptEvent(0, INITIAL.robot),
              ScriptEvent(0, Shot(5, 7)))
    assert validate_referee_configuration(INITIAL, durations=DURATIONS,
                                         events=script, cooling_42mm_per_s=2) is None
    assert INITIAL.robot.current_hp == 100


def test_maximum_script_capacity_and_uint64_offset_are_admitted():
    value = engine(events=(ScriptEvent(0, Hit(0)),) * 1023 +
                          (ScriptEvent(0xffffffffffffffff, Hit(1)),))
    value.reset(Session(7, 100))
    assert value.get_state() == INITIAL


def test_offset_zero_same_time_order_and_reset_replay_from_new_origin():
    robot = replace(INITIAL.robot, current_hp=50)
    script = (ScriptEvent(0, Hit(20)), ScriptEvent(0, robot), ScriptEvent(0, Hit(10)),
              ScriptEvent(10, Hit(5)))
    value = engine(events=script)
    value.reset(Session(7, 100))
    assert value.get_state().robot.current_hp == 40
    advance(value, 109, 1)
    assert value.get_state().robot.current_hp == 40
    advance(value, 110, 2)
    assert value.get_state().robot.current_hp == 35
    value.reset(Session(8, 1000))
    assert value.get_state().robot.current_hp == 40
    assert value.t_sim_ns == 1000
    advance(value, 1010, 1, session_id=8)
    assert value.get_state().robot.current_hp == 35


def test_explicit_hits_shots_and_buff_do_not_infer_rule_constants():
    buff = Buff(100, 400, 70, 30, 200, 0xfe)
    script = (ScriptEvent(NS // 4, buff), ScriptEvent(NS // 2, Shot(5, 7)),
              ScriptEvent(NS // 2, Hit(30)), ScriptEvent(NS // 2, Hit(100)))
    value = engine(events=script, cooling_42mm_per_s=2)
    value.reset(Session(7))
    advance(value, NS, 1)
    state = value.get_state()
    assert state.robot.current_hp == 0
    assert state.power_heat == PowerHeat(40, 12, 25)  # 10-3+5 and 20-2+7
    assert state.buff == buff
    assert state.robot.cooling_per_s == 3
    assert state.robot.power_outputs == 7


def test_heat_fraction_is_retained_and_step_partition_has_same_completed_state():
    whole, split = engine(cooling_42mm_per_s=2), engine(cooling_42mm_per_s=2)
    for value in (whole, split):
        value.reset(Session(7))
    advance(whole, 800_000_000, 1)
    for step_id, target in enumerate((100_000_000, 300_000_000, 800_000_000), 1):
        advance(split, target, step_id)
    assert whole.get_state().power_heat == PowerHeat(40, 7, 18)  # floor(7.6), floor(18.4)
    assert split.get_state() == whole.get_state()
    advance(split, 10 * NS, 4)
    assert split.get_state().power_heat == PowerHeat(40, 0, 0)


def test_power_replacement_resets_fraction_and_robot_replacement_changes_future_rate():
    power = engine(events=(ScriptEvent(200_000_000, PowerHeat(99, 10, 10)),),
                   cooling_42mm_per_s=2)
    power.reset(Session(7))
    advance(power, 100_000_000, 1)
    advance(power, 600_000_000, 2)
    assert power.get_state().power_heat == PowerHeat(99, 8, 9)  # new 10 - .4*(3,2)
    robot = replace(INITIAL.robot, cooling_per_s=10, current_hp=90)
    rates = engine(events=(ScriptEvent(300_000_000, robot),), cooling_42mm_per_s=2)
    rates.reset(Session(7))
    advance(rates, 700_000_000, 1)
    assert rates.get_state().power_heat == PowerHeat(40, 5, 18)  # 10-.3*3-.4*10
    assert rates.get_state().robot == robot


def test_phase_deadlines_use_absolute_origin_ceil_and_bounded_catchup():
    initial = replace(INITIAL, game=GameStatus(1, 2, 2, 0x0102030405060708))
    value = engine(initial)
    origin = 50 * NS
    value.reset(Session(7, origin))
    for ident, (offset, phase, remaining) in enumerate((
            (1, 2, 2), (NS, 2, 1), (2 * NS - 1, 2, 1),
            (2 * NS, 3, 3), (5 * NS, 4, 4), (9 * NS, 5, 0)), 1):
        advance(value, origin + offset, ident)
        game = value.get_state().game
        assert (game.game_progress, game.stage_remain_time) == (phase, remaining)
        assert game.sync_timestamp == 0x0102030405060708
    catchup = engine(initial)
    catchup.reset(Session(7, origin))
    advance(catchup, origin + 12 * NS, 1)
    assert catchup.get_state().game == value.get_state().game


def test_expiry_precedes_same_time_script_and_replacement_seeds_active_deadline():
    initial = replace(INITIAL, game=GameStatus(1, 2, 2, 0))
    explicit = GameStatus(1, 0, 9, 123)
    value = engine(initial, events=(ScriptEvent(2 * NS, explicit),))
    value.reset(Session(7))
    advance(value, 2 * NS, 1)
    assert value.get_state().game == explicit
    advance(value, 20 * NS, 2)
    assert value.get_state().game == explicit
    seeded = engine(initial, events=(ScriptEvent(NS, GameStatus(1, 3, 1, 456)),))
    seeded.reset(Session(7))
    advance(seeded, 2 * NS, 1)
    assert seeded.get_state().game == GameStatus(1, 4, 4, 456)


@pytest.mark.parametrize("context", [object(), StepContext(8, 1, 110, 10),
                                     StepContext(7, 2, 110, 10), StepContext(7, 1, 111, 10)])
def test_bad_context_preserves_state_and_does_not_freeze_later_valid_step(context):
    value = engine()
    value.reset(Session(7, 100))
    before = value.get_state()
    with pytest.raises(ValueError):
        value.step(context)
    assert value.get_state() is before
    assert value.t_sim_ns == 100
    assert advance(value, 110, 1) == StepResult("ok", 110)


def test_runtime_overflow_does_not_commit_earlier_event_and_freezes_until_reset():
    initial = replace(INITIAL, robot=replace(INITIAL.robot, cooling_per_s=0),
                      power_heat=PowerHeat(40, 65535, 0))
    value = engine(initial, events=(ScriptEvent(100, Hit(30)), ScriptEvent(200, Shot(1, 0))))
    value.reset(Session(7))
    advance(value, 50, 1)
    before = value.get_state()
    with pytest.raises(OverflowError):
        advance(value, 300, 2)
    assert value.get_state() is before and value.t_sim_ns == 50
    with pytest.raises(RuntimeError):
        advance(value, 100, 2)
    value.reset(Session(8, 1000))
    advance(value, 1100, 1, session_id=8)
    assert value.get_state().robot.current_hp == 70
    with pytest.raises(OverflowError):
        advance(value, 1200, 2, session_id=8)
    assert value.t_sim_ns == 1100


def test_fractional_heat_overflow_cannot_hide_in_wire_floor():
    initial = replace(INITIAL, robot=replace(INITIAL.robot, cooling_per_s=1),
                      power_heat=PowerHeat(0, 65535, 0))
    value = engine(initial, events=(ScriptEvent(NS // 2, Shot(1, 0)),))
    value.reset(Session(7))
    with pytest.raises(OverflowError):  # 65535 - .5 + 1 = 65535.5
        advance(value, NS, 1)
    assert value.get_state() == initial and value.t_sim_ns == 0


def test_existing_coordinator_and_message_envelope_consume_completed_state():
    value = engine(events=(ScriptEvent(NS // 2, Hit(30)), ScriptEvent(NS // 2, Shot(5, 7))),
                   cooling_42mm_per_s=2)
    coordinator = Coordinator({"referee": value})
    coordinator.reset(Session(7))
    assert coordinator.step(NS) == {"referee": StepResult("ok", NS)}
    state = value.get_state()
    # Official wire layouts, with independently expected HP70 and heat12/25.
    expected = ("0100000807060504030201", "03024600c80003006400500007",
                "000000000000000028000c001900", "0000000000000080")
    for message, command, payload in zip((state.game, state.robot, state.power_heat, state.buff),
                                          (0x0001, 0x0201, 0x0202, 0x0204), expected):
        frame = decode_frame(encode_message(message, sequence=0xff))
        assert (frame.command_id, frame.sequence, frame.payload) == (command, 0xff, bytes.fromhex(payload))
    assert coordinator.t_sim_ns == value.t_sim_ns == NS
    coordinator.close()
    coordinator.close()
    assert value.get_state() == state
    with pytest.raises(RuntimeError):
        value.reset(Session(8))
    with pytest.raises(RuntimeError):
        advance(value, 2 * NS, 2)


def test_snapshot_and_time_are_read_only_and_no_step_is_allowed_before_reset():
    value = engine()
    with pytest.raises(RuntimeError):
        value.get_state()
    with pytest.raises(RuntimeError):
        value.step(StepContext(7, 1, 1, 1))
    value.reset(Session(7))
    with pytest.raises(FrozenInstanceError):
        value.get_state().robot = INITIAL.robot
    with pytest.raises(AttributeError):
        value.t_sim_ns = 10
