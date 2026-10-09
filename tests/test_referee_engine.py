"""Explicit input/time contracts; numeric expectations do not call engine helpers."""

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
BAD_EVENT_BATCHES = (
    [],
    (ScriptEvent(0, Hit(0)),) * 1025,
    (ScriptEvent(1, Hit(0)), ScriptEvent(0, Hit(0))),
    (ScriptEvent(True, Hit(0)),),
    (ScriptEvent(1 << 64, Hit(0)),),
    (ScriptEvent(0, object()),),
    (ScriptEvent(0, Hit(True)),),
    (ScriptEvent(0, Shot(-1, 0)),),
    (ScriptEvent(0, Shot(0, 65536)),),
    (ScriptEvent(0, GameStatus(1, 4, 0, 0)),),
    (ScriptEvent(0, replace(INITIAL.robot, current_hp=201)),),
)


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
    *((INITIAL, {"events": batch}) for batch in BAD_EVENT_BATCHES),
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


@pytest.mark.parametrize("live", [False, True], ids=["script", "queued"])
def test_explicit_hits_shots_and_buff_do_not_infer_rule_constants(live):
    buff = Buff(100, 400, 70, 30, 200, 0xfe)
    script = (ScriptEvent(NS // 4, buff), ScriptEvent(NS // 2, Shot(5, 7)),
              ScriptEvent(NS // 2, Hit(30)), ScriptEvent(NS // 2, Hit(100)))
    value = engine(events=() if live else script, cooling_42mm_per_s=2)
    value.reset(Session(7))
    if live:
        value.queue_events(script, session_id=7)
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


@pytest.mark.parametrize("live", [False, True], ids=["script", "queued"])
def test_power_replacement_resets_fraction_and_robot_replacement_changes_future_rate(live):
    power_events = (ScriptEvent(200_000_000, PowerHeat(99, 10, 10)),)
    power = engine(events=() if live else power_events,
                   cooling_42mm_per_s=2)
    power.reset(Session(7))
    if live:
        power.queue_events(power_events, session_id=7)
    advance(power, 100_000_000, 1)
    advance(power, 600_000_000, 2)
    assert power.get_state().power_heat == PowerHeat(99, 8, 9)  # new 10 - .4*(3,2)
    robot = replace(INITIAL.robot, cooling_per_s=10, current_hp=90)
    robot_events = (ScriptEvent(300_000_000, robot),)
    rates = engine(events=() if live else robot_events, cooling_42mm_per_s=2)
    rates.reset(Session(7))
    if live:
        rates.queue_events(robot_events, session_id=7)
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


@pytest.mark.parametrize("live", [False, True], ids=["script", "queued"])
def test_expiry_precedes_same_time_input_and_replacement_seeds_active_deadline(live):
    initial = replace(INITIAL, game=GameStatus(1, 2, 2, 0))
    explicit = GameStatus(1, 0, 9, 123)
    events = (ScriptEvent(2 * NS, explicit),)
    value = engine(initial, events=() if live else events)
    value.reset(Session(7))
    if live:
        value.queue_events(events, session_id=7)
    advance(value, 2 * NS, 1)
    assert value.get_state().game == explicit
    advance(value, 20 * NS, 2)
    assert value.get_state().game == explicit
    events = (ScriptEvent(NS, GameStatus(1, 3, 1, 456)),)
    seeded = engine(initial, events=() if live else events)
    seeded.reset(Session(7))
    if live:
        seeded.queue_events(events, session_id=7)
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


@pytest.mark.parametrize("live", [False, True], ids=["script", "queued"])
def test_runtime_overflow_does_not_commit_earlier_event_and_freezes_until_reset(live):
    initial = replace(INITIAL, robot=replace(INITIAL.robot, cooling_per_s=0),
                      power_heat=PowerHeat(40, 65535, 0))
    events = (ScriptEvent(100, Hit(30)), ScriptEvent(200, Shot(1, 0)))
    value = engine(initial, events=() if live else events)
    value.reset(Session(7))
    if live:
        value.queue_events(events, session_id=7)
    advance(value, 50, 1)
    before = value.get_state()
    with pytest.raises(OverflowError):
        advance(value, 300, 2)
    assert value.get_state() is before and value.t_sim_ns == 50
    with pytest.raises(RuntimeError):
        advance(value, 100, 2)
    with pytest.raises(RuntimeError):
        value.queue_events((), session_id=7)
    value.reset(Session(8, 1000))
    if live:
        value.queue_events(events, session_id=8)
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


@pytest.mark.parametrize("live", [False, True], ids=["script", "queued"])
def test_existing_coordinator_and_message_envelope_consume_completed_state(live):
    from sim.devices.referee import RefereeUartEndpoint
    from sim.transports import StreamTransport

    events = (ScriptEvent(NS // 2, Hit(30)), ScriptEvent(NS // 2, Shot(5, 7)))
    value = engine(events=() if live else events,
                   cooling_42mm_per_s=2)
    coordinator = Coordinator({"referee": value})
    coordinator.reset(Session(7))
    if live:
        value.queue_events(events, session_id=7)
    assert coordinator.step(NS) == {"referee": StepResult("ok", NS)}
    state = value.get_state()
    # Official wire layouts, with independently expected HP70 and heat12/25.
    expected = ("0100000807060504030201", "03024600c80003006400500007",
                "000000000000000028000c001900", "0000000000000080")
    for message, command, payload in zip((state.game, state.robot, state.power_heat, state.buff),
                                          (0x0001, 0x0201, 0x0202, 0x0204), expected):
        frame = decode_frame(encode_message(message, sequence=0xff))
        assert (frame.command_id, frame.sequence, frame.payload) == (command, 0xff, bytes.fromhex(payload))
    sender, peer = socket.socketpair()
    sender.setblocking(False)
    peer.settimeout(1)
    endpoint = RefereeUartEndpoint(StreamTransport(sender), sequence=0xff)
    try:
        wire = endpoint.send_state(state)
        assert peer.recv(4096) == wire
        cursor = 0
        for index, (command, payload_hex) in enumerate(zip((0x0001, 0x0201, 0x0202, 0x0204), expected)):
            payload = bytes.fromhex(payload_hex)
            end = cursor + len(payload) + 9
            frame = decode_frame(wire[cursor:end])
            assert (frame.command_id, frame.sequence, frame.payload) == (command, (0xff + index) % 256, payload)
            cursor = end
        assert cursor == len(wire) == 82 and endpoint.next_sequence == 3
    finally:
        try:
            endpoint.close()
            assert sender.fileno() >= 0  # Endpoint borrows; this consumer owns sockets.
        finally:
            sender.close()
            peer.close()
    assert coordinator.t_sim_ns == value.t_sim_ns == NS
    coordinator.close()
    coordinator.close()
    assert value.get_state() == state
    with pytest.raises(RuntimeError):
        value.reset(Session(8))
    with pytest.raises(RuntimeError):
        advance(value, 2 * NS, 2)
    with pytest.raises(RuntimeError):
        value.queue_events((), session_id=7)


def test_snapshot_and_time_are_read_only_and_no_step_is_allowed_before_reset():
    value = engine()
    with pytest.raises(RuntimeError):
        value.get_state()
    with pytest.raises(RuntimeError):
        value.step(StepContext(7, 1, 1, 1))
    with pytest.raises(RuntimeError):
        value.queue_events((), session_id=7)
    value.reset(Session(7))
    with pytest.raises(FrozenInstanceError):
        value.get_state().robot = INITIAL.robot
    with pytest.raises(AttributeError):
        value.t_sim_ns = 10


def test_queue_admission_preserves_snapshot_and_equal_completed_offset_waits_for_next_step():
    value = engine()
    value.reset(Session(7, 100))
    before = value.get_state()
    assert value.queue_events((), session_id=7) is None
    assert value.queue_events((ScriptEvent(0, Hit(10)),), session_id=7) is None
    assert value.get_state() is before and value.t_sim_ns == 100
    with pytest.raises(ValueError):
        value.step(StepContext(7, 2, 110, 10))
    value.queue_events((ScriptEvent(0, Hit(5)),), session_id=7)
    advance(value, 110, 1)
    assert value.get_state().robot.current_hp == 85
    completed = value.get_state()
    value.queue_events((ScriptEvent(10, Hit(5)),), session_id=7)
    assert value.get_state() is completed and value.t_sim_ns == 110
    advance(value, 111, 2)
    assert value.get_state().robot.current_hp == 80


def test_static_and_live_events_merge_chronologically_with_static_first_ties_and_live_fifo():
    initial = replace(INITIAL, game=GameStatus(1, 2, 1, 123))
    robot = replace(INITIAL.robot, current_hp=60, cooling_per_s=10)
    buff = Buff(5, 9, 7, 11, 13, 0xfe)
    value = engine(initial, cooling_42mm_per_s=2, events=(
        ScriptEvent(NS // 4, Hit(10)),
        ScriptEvent(NS, GameStatus(1, 0, 9, 111)),
        ScriptEvent(NS, replace(INITIAL.robot, current_hp=90)),
        ScriptEvent(NS, PowerHeat(40, 100, 200))))
    origin = 50 * NS
    value.reset(Session(7, origin))
    value.queue_events((ScriptEvent(NS // 2, Hit(20)),
        ScriptEvent(NS, GameStatus(2, 3, 2, 222)), ScriptEvent(NS, robot),
        ScriptEvent(NS, PowerHeat(55, 10, 20)), ScriptEvent(NS, buff),
        ScriptEvent(NS, Shot(5, 7)), ScriptEvent(NS, Hit(5))), session_id=7)
    value.queue_events((ScriptEvent(NS, Hit(7)),), session_id=7)
    advance(value, origin + NS // 2, 1)
    assert value.get_state().robot.current_hp == 70
    advance(value, origin + NS, 2)
    state = value.get_state()
    assert state == RefereeState(GameStatus(2, 3, 2, 222), replace(robot, current_hp=48),
                                 PowerHeat(55, 15, 27), buff)
    advance(value, origin + 1_200_000_000, 3)
    assert value.get_state().power_heat == PowerHeat(55, 13, 26)  # 15-.2*10, floor(27-.2*2)
    advance(value, origin + 3 * NS, 4)
    assert value.get_state().game == GameStatus(2, 4, 4, 222)


def test_cooling_precedes_live_shot_at_exact_unsigned_heat_limit():
    initial = replace(INITIAL, robot=replace(INITIAL.robot, cooling_per_s=1),
                      power_heat=PowerHeat(40, 65535, 0))
    value = engine(initial)
    value.reset(Session(7))
    value.queue_events((ScriptEvent(NS, Shot(1, 0)),), session_id=7)
    advance(value, NS, 1)
    assert value.get_state().power_heat == PowerHeat(40, 65535, 0)  # 65535-1+1


@pytest.mark.parametrize("bad_batch", BAD_EVENT_BATCHES)
def test_full_dynamic_batch_validation_is_atomic_and_reuses_existing_typed_admission(bad_batch):
    value = engine()
    value.reset(Session(7, 100))
    value.queue_events((ScriptEvent(0, Hit(10)),), session_id=7)
    before = value.get_state()
    batch = ((ScriptEvent(0, Hit(30)), *bad_batch)
             if type(bad_batch) is tuple else [ScriptEvent(0, Hit(30)), *bad_batch])
    with pytest.raises((ValueError, TypeError)):
        value.queue_events(batch, session_id=7)
    assert value.get_state() is before and value.t_sim_ns == 100
    value.queue_events((ScriptEvent(0, Hit(5)),), session_id=7)
    advance(value, 110, 1)
    assert value.get_state().robot.current_hp == 85  # prior10 + subsequent5, no refused30


@pytest.mark.parametrize("rejection", ["session", "session_type", "past", "pending_past", "reverse_batch"])
def test_session_and_chronology_refusals_preserve_pending_state_and_do_not_freeze(rejection):
    value = engine()
    value.reset(Session(7, 100))
    advance(value, 105, 1)
    if rejection != "past":
        value.queue_events((ScriptEvent(10, Hit(10)),), session_id=7)
    before = value.get_state()
    batch, session_id = (ScriptEvent(10, Hit(30)),), 7
    if rejection == "session":
        session_id = 8
    elif rejection == "session_type":
        session_id = 7.0
    elif rejection == "past":
        batch = (ScriptEvent(4, Hit(30)),)
    elif rejection == "pending_past":
        batch = (ScriptEvent(9, Hit(30)),)
    else:
        batch = (ScriptEvent(11, Hit(30)), ScriptEvent(10, Hit(30)))
    with pytest.raises((ValueError, TypeError)):
        value.queue_events(batch, session_id=session_id)
    assert value.get_state() is before and value.t_sim_ns == 105
    value.queue_events((ScriptEvent(10, Hit(5)),), session_id=7)
    advance(value, 112, 2)
    assert value.get_state().robot.current_hp == (95 if rejection == "past" else 85)


def test_dynamic_capacity_is_separate_from_static_script_and_released_only_by_completed_step():
    value = engine(events=(ScriptEvent(0, Hit(0)),) * 1024)
    value.reset(Session(7, 100))
    value.queue_events((ScriptEvent(10, Hit(0)),) * 1023 + (ScriptEvent(20, Hit(5)),), session_id=7)
    before = value.get_state()
    with pytest.raises(BufferError):
        value.queue_events((ScriptEvent(20, Hit(30)),), session_id=7)
    assert value.get_state() is before and value.t_sim_ns == 100
    advance(value, 110, 1)
    value.queue_events((ScriptEvent(20, Hit(0)),) * 1023, session_id=7)
    advance(value, 120, 2)
    assert value.get_state().robot.current_hp == 95


def test_invalid_reset_preserves_dynamic_inputs_successful_reset_cancels_them_and_static_replays():
    value = engine(events=(ScriptEvent(0, Hit(20)), ScriptEvent(10, Hit(5))))
    value.reset(Session(7, 100))
    value.queue_events((ScriptEvent(5, Hit(10)), ScriptEvent(20, Hit(100))), session_id=7)
    before = value.get_state()
    with pytest.raises(TypeError):
        value.reset(object())
    assert value.get_state() is before and value.t_sim_ns == 100
    advance(value, 105, 1)
    assert value.get_state().robot.current_hp == 70
    value.reset(Session(8, 1000))
    assert value.get_state().robot.current_hp == 80
    advance(value, 1020, 1, session_id=8)
    assert value.get_state().robot.current_hp == 75
    value.queue_events((ScriptEvent(20, Hit(30)),), session_id=8)
    completed = value.get_state()
    value.close()
    assert value.closed and value.get_state() is completed
    with pytest.raises(RuntimeError):
        value.queue_events((), session_id=8)
