"""Explicit referee event fixture, independent of boards and world backends.

Heat arithmetic is a narrow port of rm_control's BSD-3-Clause HeatLimit
heatCB/timerCB at c5205eb193a8cda6147efe760eafedd0a3ed0104 (Qiayuan Liao,
2021). Changes: explicit heat increments, integer elapsed virtual nanoseconds,
no ROS/timer/mutex/protection policy. Generic phase2->3->4->5 progression is
adapted from XDU-IRobot/rm_referee_ros2's Apache-2.0 MatchControlTiming at
45da10a5317d47cc4b5d5968ec553946c43ebe0d. Changes: explicit durations and
absolute virtual deadlines, no Qt/widgets or V2 rules. See adjacent licenses.

This is not competition adjudication: damage, power and buffs are explicit
inputs. There is no automatic overheat punishment, revival or physics judgment.
"""

from dataclasses import dataclass, replace

from sim.core import Session, StepContext, StepResult

from .messages import Buff, GameStatus, PowerHeat, RobotStatus, encode_message
from .protocol import _uint


_NS = 1_000_000_000
_MAX_HEAT = 65535 * _NS
MAX_SCRIPT_EVENTS = 1024


@dataclass(frozen=True)
class RefereeState:
    game: GameStatus
    robot: RobotStatus
    power_heat: PowerHeat
    buff: Buff


@dataclass(frozen=True)
class MatchDurations:
    countdown_s: int
    battle_s: int


@dataclass(frozen=True)
class Hit:
    damage_hp: int


@dataclass(frozen=True)
class Shot:
    heat_17mm: int
    heat_42mm: int


@dataclass(frozen=True)
class ScriptEvent:
    offset_ns: int
    value: Hit | Shot | GameStatus | RobotStatus | PowerHeat | Buff


def _admit_message(value, expected_type):
    if type(value) is not expected_type:
        raise TypeError(f"expected {expected_type.__name__}")
    # Borrow the wire owner's admission instead of maintaining a second schema.
    encode_message(value)
    if expected_type is RobotStatus and value.current_hp > value.maximum_hp:
        raise ValueError("current_hp must not exceed maximum_hp")
    if (expected_type is GameStatus and value.game_progress in (2, 3, 4)
            and value.stage_remain_time == 0):
        raise ValueError("active game phase requires positive remaining seconds")


def _deadline(game, now_ns):
    return (now_ns + game.stage_remain_time * _NS
            if game.game_progress in (2, 3, 4) else None)


def validate_referee_configuration(initial: RefereeState, *, durations: MatchDurations,
                                   events: tuple[ScriptEvent, ...] = (),
                                   cooling_42mm_per_s: int = 0) -> None:
    """Admit a fixture configuration without constructing or executing an owner."""
    if type(initial) is not RefereeState:
        raise TypeError("initial must be a RefereeState")
    for value, expected in ((initial.game, GameStatus), (initial.robot, RobotStatus),
                            (initial.power_heat, PowerHeat), (initial.buff, Buff)):
        _admit_message(value, expected)
    if type(durations) is not MatchDurations:
        raise TypeError("durations must be MatchDurations")
    for value, label in ((durations.countdown_s, "countdown_s"),
                         (durations.battle_s, "battle_s")):
        if _uint(value, 65535, label) == 0:
            raise ValueError(f"{label} must be positive")
    _uint(cooling_42mm_per_s, 65535, "cooling_42mm_per_s")
    validate_referee_events(events)


def validate_referee_events(events: tuple[ScriptEvent, ...]) -> None:
    """Validate immutable typed events without constructing an engine."""
    if type(events) is not tuple:
        raise TypeError("events must be an immutable tuple")
    if len(events) > MAX_SCRIPT_EVENTS:
        raise ValueError(f"script exceeds {MAX_SCRIPT_EVENTS} events")
    previous = 0
    for event in events:
        if type(event) is not ScriptEvent:
            raise TypeError("script requires ScriptEvent values")
        _uint(event.offset_ns, 0xffffffffffffffff, "event offset_ns")
        if event.offset_ns < previous:
            raise ValueError("script offsets must be nondecreasing")
        previous = event.offset_ns
        if type(event.value) is Hit:
            _uint(event.value.damage_hp, 65535, "damage_hp")
        elif type(event.value) is Shot:
            _uint(event.value.heat_17mm, 65535, "heat_17mm increment")
            _uint(event.value.heat_42mm, 65535, "heat_42mm increment")
        elif type(event.value) in (GameStatus, RobotStatus, PowerHeat, Buff):
            _admit_message(event.value, type(event.value))
        else:
            raise TypeError("unsupported referee script event")


class RefereeEngine:
    """Single-owner participant with immutable snapshots and bounded events.

    Script offsets are relative to each reset origin. Due events apply once in
    tuple order, after cooling/phase expiry at their timestamp. Queued events use
    the same offsets, after static events on ties and in admission order. Only
    successful reset/step publishes a snapshot; calculation failure freezes until reset.
    No method owns a clock, transport, sequence number or external resource.
    """

    def __init__(self, initial: RefereeState, *, durations: MatchDurations,
                 events: tuple[ScriptEvent, ...] = (), cooling_42mm_per_s: int = 0):
        validate_referee_configuration(initial, durations=durations, events=events,
                                       cooling_42mm_per_s=cooling_42mm_per_s)
        self._initial = initial
        self._durations = durations
        self._events = events
        self._pending = ()
        self._cooling_42mm_per_s = cooling_42mm_per_s
        self._session = None
        self._t_sim_ns = 0
        self._step_id = 0
        self._runtime = None
        self._failed = True
        self._closed = False

    @property
    def t_sim_ns(self):
        return self._t_sim_ns

    @property
    def closed(self):
        return self._closed

    def get_state(self) -> RefereeState:
        """Read the latest completed snapshot, including after failure/close."""
        if self._runtime is None:
            raise RuntimeError("referee has no completed reset")
        return self._runtime[0]

    def queue_events(self, events: tuple[ScriptEvent, ...], *, session_id: int) -> None:
        """Admit events for the active session without changing time or snapshot.

        Append in nondecreasing offset order, at or after the completed time.
        Events at the completed time apply at the next step's start. Capacity
        refusal preserves the queue and permits a later step to release space.
        Only successful reset or close cancels pending events. Calls must be
        serialized with reset/step. The caller owns source time and deduplication.
        """
        if self._closed or self._failed or self._session is None:
            raise RuntimeError("referee requires a successful reset")
        if type(session_id) is not int or session_id != self._session.session_id:
            raise ValueError("referee events must match the active session")
        validate_referee_events(events)
        minimum = (self._pending[-1].offset_ns if self._pending
                   else self._t_sim_ns - self._session.t_sim_ns)
        if events and events[0].offset_ns < minimum:
            raise ValueError("referee events must follow completed time and pending order")
        if len(self._pending) + len(events) > MAX_SCRIPT_EVENTS:
            raise BufferError(f"pending referee events exceed {MAX_SCRIPT_EVENTS}")
        self._pending += events

    def reset(self, session: Session) -> None:
        if self._closed:
            raise RuntimeError("referee is closed")
        if not isinstance(session, Session):
            raise TypeError("reset requires a Session")
        self._failed = True
        runtime = (self._initial, self._initial.power_heat.heat_17mm * _NS,
                   self._initial.power_heat.heat_42mm * _NS,
                   _deadline(self._initial.game, session.t_sim_ns), 0)
        runtime, _ = self._simulate(runtime, session.t_sim_ns, session.t_sim_ns,
                                    session.t_sim_ns, ())
        self._runtime = runtime
        self._session = session
        self._t_sim_ns = session.t_sim_ns
        self._step_id = 0
        self._pending = ()
        self._failed = False

    def step(self, ctx: StepContext) -> StepResult:
        if self._closed or self._failed or self._session is None:
            raise RuntimeError("referee requires a successful reset")
        if (not isinstance(ctx, StepContext) or ctx.session_id != self._session.session_id
                or ctx.step_id != self._step_id + 1
                or ctx.t_sim_ns != self._t_sim_ns + ctx.dt_ns):
            raise ValueError("referee step must follow its session and exact target")
        self._failed = True
        runtime, consumed = self._simulate(self._runtime, self._session.t_sim_ns,
                                           self._t_sim_ns, ctx.t_sim_ns, self._pending)
        pending = self._pending[consumed:]
        self._runtime = runtime
        self._t_sim_ns = ctx.t_sim_ns
        self._step_id = ctx.step_id
        self._pending = pending
        self._failed = False
        return StepResult("ok", ctx.t_sim_ns)

    def _phase_to(self, game, deadline, target):
        # Narrow phase kernel: 2->3->4->5, with no per-second catch-up loop.
        while deadline is not None and target >= deadline:
            phase = game.game_progress + 1
            if phase == 5:
                game = replace(game, game_progress=5, stage_remain_time=0)
                deadline = None
            else:
                seconds = (self._durations.countdown_s if phase == 3
                           else self._durations.battle_s)
                game = replace(game, game_progress=phase, stage_remain_time=seconds)
                deadline += seconds * _NS
        if deadline is not None:
            game = replace(game, stage_remain_time=(deadline - target + _NS - 1) // _NS)
        return game, deadline

    def _simulate(self, runtime, origin, start, target, pending):
        # All calculations are local: an overflow cannot publish a partial state.
        state, heat17, heat42, deadline, cursor = runtime
        consumed = 0
        now = start
        while True:
            event = self._events[cursor] if cursor < len(self._events) else None
            queued = pending[consumed] if consumed < len(pending) else None
            from_queue = queued is not None and (event is None
                                                 or queued.offset_ns < event.offset_ns)
            if from_queue:
                event = queued
            due = origin + event.offset_ns if event is not None else None
            at = target if due is None or due > target else due
            elapsed = at - now
            # Port of HeatLimit::timerCB: rate*dt then saturate at zero.
            heat17 = max(0, heat17 - state.robot.cooling_per_s * elapsed)
            heat42 = max(0, heat42 - self._cooling_42mm_per_s * elapsed)
            game, deadline = self._phase_to(state.game, deadline, at)
            state = replace(state, game=game)
            now = at
            if due is None or due > target:
                break
            value = event.value
            if type(value) is Hit:
                state = replace(state, robot=replace(state.robot,
                                current_hp=max(0, state.robot.current_hp - value.damage_hp)))
            elif type(value) is Shot:
                # Port of HeatLimit::heatCB, with explicit event increments.
                heat17 += value.heat_17mm * _NS
                heat42 += value.heat_42mm * _NS
                if heat17 > _MAX_HEAT or heat42 > _MAX_HEAT:
                    raise OverflowError("referee heat exceeds the unsigned16 profile")
            elif type(value) is GameStatus:
                state = replace(state, game=value)
                deadline = _deadline(value, at)
            elif type(value) is RobotStatus:
                state = replace(state, robot=value)
            elif type(value) is PowerHeat:
                state = replace(state, power_heat=value)
                heat17, heat42 = value.heat_17mm * _NS, value.heat_42mm * _NS
            else:  # Buff was admitted before execution.
                state = replace(state, buff=value)
            if from_queue:
                consumed += 1
            else:
                cursor += 1
        state = replace(state, power_heat=replace(state.power_heat,
                        heat_17mm=heat17 // _NS, heat_42mm=heat42 // _NS))
        return (state, heat17, heat42, deadline, cursor), consumed

    def close(self) -> None:
        self._closed = True
        self._pending = ()
