"""Pure Scenario admission for scoped inputs and observation assertions.

Event times are relative to the shared host reset. Duration is the Arena active
duration after requested ready markers; startup events may precede that origin.
The runner delegates referee and CAN policy execution to the existing Arena
owner. Assertions consume actual per-member QEMU observations. This module
reads configuration only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sim.arena.manifest import (
    MAX_CAN_DELAY_EVENTS, MAX_MOTOR_FEEDBACK_EVENTS,
    ArenaManifest, load_manifest as load_arena,
    member_uarts, parse_referee_events, validate_can_delays,
)
from sim.lab.manifest import Assertion, MAX_ITEMS, parse_assertion
from sim.manifest import (
    ManifestError, fields, file_path, identifier, load_document, parse_time,
)

if TYPE_CHECKING:
    from sim.devices.referee import ScriptEvent


@dataclass(frozen=True)
class ScenarioReferee:
    member: str
    events: tuple[ScriptEvent, ...]


@dataclass(frozen=True)
class ScenarioCanDelay:
    offset_ns: int
    member: str
    delay_ns: int


@dataclass(frozen=True)
class ScenarioMotorFeedback:
    offset_ns: int
    member: str
    motor_id: int
    enabled: bool


@dataclass(frozen=True)
class ScenarioAssertion:
    member: str
    assertion: Assertion


@dataclass(frozen=True)
class ScenarioManifest:
    path: Path
    arena: ArenaManifest
    duration_ns: int
    referees: tuple[ScenarioReferee, ...]
    can_delays: tuple[ScenarioCanDelay, ...] = ()
    motor_feedback: tuple[ScenarioMotorFeedback, ...] = ()
    assertions: tuple[ScenarioAssertion, ...] = ()


def _assertions(value, duration_ns):
    if type(value) is not list or len(value) > MAX_ITEMS:
        raise ManifestError(f"assertions must be a list with at most {MAX_ITEMS} entries")
    records = []
    for index, configuration in enumerate(value):
        label = f"assertions[{index}]"
        item = fields(configuration, ("member", "before", "uart_contains", "digital_equals"),
                      ("member", "before"), label)
        member = identifier(item["member"], label + ".member")
        assertion = parse_assertion({key: val for key, val in item.items() if key != "member"}, index)
        if assertion.before_ns > duration_ns:
            raise ManifestError(f"{label}.before must not exceed duration")
        records.append(ScenarioAssertion(member, assertion))
    return tuple(records)


def _assertion_members(assertions, arena):
    robots = {item.name: item for item in arena.robots}
    members = {item.member for item in arena.referees}
    if assertions and len(robots) != len(arena.robots):
        raise ManifestError("Scenario assertion members must be unique")
    for index, entry in enumerate(assertions):
        label = f"assertions[{index}]"
        if entry.member not in robots:
            raise ManifestError(f"{label}.member {entry.member!r} must name an Arena member")
        if (entry.assertion.kind == "uart_contains"
                and entry.assertion.endpoint not in member_uarts(
                    robots[entry.member], referee=entry.member in members)):
            raise ManifestError(f"{label} UART endpoint {entry.assertion.endpoint!r} "
                                f"is not configured for Arena member {entry.member!r}")


def validate_assertions(assertions, *, arena: ArenaManifest, duration_ns: int) -> None:
    """Admit typed conditions and their actual observable member endpoints.

    Frozen records can be constructed directly. Round-trip their plain values
    through the same public Lab parser, retaining the original records and order.
    No files, runtime modules or resources are accessed here.
    """
    if (type(assertions) is not tuple
            or any(type(item) is not ScenarioAssertion for item in assertions)):
        raise TypeError("Scenario assertions require a plain ScenarioAssertion tuple")
    if len(assertions) > MAX_ITEMS:
        raise ManifestError(f"assertions must have at most {MAX_ITEMS} entries")
    if not assertions:
        return
    for index, entry in enumerate(assertions):
        label = f"assertions[{index}]"
        identifier(entry.member, label + ".member")
        assertion = entry.assertion
        if type(assertion) is not Assertion:
            raise TypeError(f"{label} requires a plain Lab Assertion")
        if (type(assertion.before_ns) is not int
                or type(assertion.endpoint) is not str or type(assertion.kind) is not str):
            raise TypeError(f"{label} requires a plain integer deadline and string endpoint/kind")
        if not 1 <= assertion.before_ns <= duration_ns:
            raise ManifestError(f"{label}.before must be positive and not exceed duration")
        condition = {"endpoint": assertion.endpoint}
        if assertion.kind == "uart_contains":
            if type(assertion.value) is not bytes:
                raise TypeError(f"{label} UART text must be plain bytes")
            try:
                condition["text"] = assertion.value.decode("utf-8")
            except UnicodeError as error:
                raise ManifestError(f"{label} UART text must be valid UTF-8") from error
        elif assertion.kind == "digital_equals":
            condition["value"] = assertion.value
        else:
            raise ManifestError(f"{label} unsupported assertion kind: {assertion.kind!r}")
        parse_assertion({"before": f"{assertion.before_ns}ns", assertion.kind: condition}, index)
    _assertion_members(assertions, arena)


def _events(value, duration_ns):
    limit = MAX_CAN_DELAY_EVENTS + MAX_MOTOR_FEEDBACK_EVENTS
    if type(value) is not list or len(value) > limit:
        raise ManifestError(f"events must be a list with at most {limit} entries")
    if not value:
        return (), (), ()
    records, targets, feedback = [], [], []
    previous_offset = 0
    for index, configuration in enumerate(value):
        label = f"events[{index}]"
        required = ("at", "action", "target", "value")
        event = fields(configuration, required, required, label)
        if type(event["action"]) is not str or event["action"] not in ("can_delay", "motor_feedback"):
            raise ManifestError(f"{label}.action unsupported: {event['action']!r}")
        target = event["target"]
        if type(target) is not str or target.count(".") != 1:
            raise ManifestError(f"{label}.target must be a scoped member.endpoint name")
        member, endpoint = target.split(".")
        identifier(member, label + ".target.member")
        identifier(endpoint, label + ".target.endpoint")
        offset = parse_time(event["at"], label + ".at")
        if offset < previous_offset or offset > duration_ns:
            raise ManifestError(f"{label}.at must be nondecreasing and not exceed duration")
        previous_offset = offset
        if event["action"] == "can_delay":
            if len(records) >= MAX_CAN_DELAY_EVENTS:
                raise ManifestError(f"events CAN delay actions must not exceed {MAX_CAN_DELAY_EVENTS}")
            records.append(ScenarioCanDelay(offset, member, parse_time(event["value"], label + ".value")))
            targets.append((index, target))
        else:
            if len(feedback) >= MAX_MOTOR_FEEDBACK_EVENTS:
                raise ManifestError(f"events motor feedback actions must not exceed {MAX_MOTOR_FEEDBACK_EVENTS}")
            if type(event["value"]) is not bool:
                raise ManifestError(f"{label}.value must be a boolean")
            feedback.append((index, offset, member, endpoint, event["value"]))
    records = tuple(records)
    try:
        validate_can_delays(tuple((item.offset_ns, item.member, item.delay_ns)
                                 for item in records), duration_ns=duration_ns)
    except (TypeError, ValueError) as error:
        raise ManifestError(f"events: {error}") from error
    return records, tuple(targets), tuple(feedback)


def _referee_events(value, duration_ns):
    if type(value) is not dict:
        raise ManifestError("referee_events must be a mapping")
    if not value:
        return {}
    from sim.devices.referee.engine import MAX_SCRIPT_EVENTS

    result = {}
    count = 0
    for member, script in value.items():
        identifier(member, "referee_events label")
        label = f"referee_events.{member}"
        if type(script) is not list:
            raise ManifestError(f"{label} must be a list")
        count += len(script)
        if count > MAX_SCRIPT_EVENTS:
            raise ManifestError(f"referee_events total must not exceed {MAX_SCRIPT_EVENTS}")
        converted = []
        for index, configuration in enumerate(script):
            event_label = f"{label}[{index}]"
            event = fields(configuration, ("at", "hit", "shot", "game", "robot",
                                           "power_heat", "buff"), ("at",), event_label)
            at_ns = parse_time(event["at"], event_label + ".at")
            if at_ns > duration_ns:
                raise ManifestError(f"{event_label}.at must not exceed duration")
            converted.append({"offset_ns": at_ns,
                              **{key: item for key, item in event.items() if key != "at"}})
        result[member] = parse_referee_events(converted, label)
    return result


def load_manifest(path: str | Path) -> ScenarioManifest:
    """Return immutable inputs without acquiring or executing simulation owners."""
    manifest_path, document = load_document(path)
    required = ("schema", "arena", "duration")
    item = fields(document, (*required, "referee_events", "events", "assertions"),
                  required, "scenario manifest")
    if type(item["schema"]) is not int or item["schema"] != 1:
        raise ManifestError("unsupported Scenario schema; schema must be integer 1")
    duration_ns = parse_time(item["duration"], "duration", zero=False)
    events = _referee_events(item.get("referee_events", {}), duration_ns)
    can_delays, targets, feedback = _events(item.get("events", []), duration_ns)
    assertions = _assertions(item.get("assertions", []), duration_ns)
    try:
        reference = file_path(item["arena"], manifest_path.parent, "scenario.arena")
        arena = load_arena(reference)
    except ManifestError as error:
        raise ManifestError(f"scenario.arena {item['arena']!r}: {error}") from error
    members = {item.member for item in arena.referees}
    for member in events:
        if member not in members:
            raise ManifestError(f"referee_events.{member} must name an Arena member with a referee")
    referees = tuple(ScenarioReferee(item.member, events[item.member])
                     for item in arena.referees if item.member in events)
    robots = {item.name: item for item in arena.robots}
    _assertion_members(assertions, arena)
    for action, (index, target) in zip(can_delays, targets):
        if (action.member not in robots
                or target != action.member + "." + robots[action.member].robot.bus.name):
            raise ManifestError(f"events[{index}].target {target!r} must name its Arena member's bus")
    motor_feedback = []
    for index, offset, member, name, enabled in feedback:
        motors = (() if member not in robots else
                  tuple(motor for motor in robots[member].robot.devices if motor.name == name))
        if (len(motors) != 1 or type(motors[0].motor_id) is not int
                or motors[0].bus != robots[member].robot.bus.name):
            raise ManifestError(f"events[{index}].target {member + '.' + name!r} must name its Arena member's motor")
        motor_feedback.append(ScenarioMotorFeedback(offset, member, motors[0].motor_id, enabled))
    return ScenarioManifest(manifest_path, arena, duration_ns, referees, can_delays,
                            tuple(motor_feedback), assertions)
