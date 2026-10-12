"""Pure Scenario admission for explicit scoped referee and CAN policy events.

Event times are relative to the shared host reset. Duration is the Arena active
duration after requested ready markers; startup events may precede that origin.
The referee engine owns domain execution; CAN policy execution is not yet
wired into the Scenario runner. This module reads configuration only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sim.arena.manifest import (
    MAX_CAN_DELAY_EVENTS, ArenaManifest, load_manifest as load_arena,
    parse_referee_events, validate_can_delays,
)
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
class ScenarioManifest:
    path: Path
    arena: ArenaManifest
    duration_ns: int
    referees: tuple[ScenarioReferee, ...]
    can_delays: tuple[ScenarioCanDelay, ...] = ()


def _can_delays(value, duration_ns):
    if type(value) is not list or len(value) > MAX_CAN_DELAY_EVENTS:
        raise ManifestError(f"events must be a list with at most {MAX_CAN_DELAY_EVENTS} entries")
    if not value:
        return (), ()
    records, targets = [], []
    for index, configuration in enumerate(value):
        label = f"events[{index}]"
        required = ("at", "action", "target", "value")
        event = fields(configuration, required, required, label)
        if type(event["action"]) is not str or event["action"] != "can_delay":
            raise ManifestError(f"{label}.action unsupported: {event['action']!r}")
        target = event["target"]
        if type(target) is not str or target.count(".") != 1:
            raise ManifestError(f"{label}.target must be a scoped member.bus name")
        member, bus = target.split(".")
        identifier(member, label + ".target.member")
        identifier(bus, label + ".target.bus")
        records.append(ScenarioCanDelay(parse_time(event["at"], label + ".at"), member,
                                        parse_time(event["value"], label + ".value")))
        targets.append(target)
    records = tuple(records)
    try:
        validate_can_delays(tuple((item.offset_ns, item.member, item.delay_ns)
                                 for item in records), duration_ns=duration_ns)
    except (TypeError, ValueError) as error:
        raise ManifestError(f"events: {error}") from error
    return records, tuple(targets)


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
    item = fields(document, (*required, "referee_events", "events"), required, "scenario manifest")
    if type(item["schema"]) is not int or item["schema"] != 1:
        raise ManifestError("unsupported Scenario schema; schema must be integer 1")
    duration_ns = parse_time(item["duration"], "duration", zero=False)
    events = _referee_events(item.get("referee_events", {}), duration_ns)
    can_delays, targets = _can_delays(item.get("events", []), duration_ns)
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
    for index, (action, target) in enumerate(zip(can_delays, targets)):
        if (action.member not in robots
                or target != action.member + "." + robots[action.member].robot.bus.name):
            raise ManifestError(f"events[{index}].target {target!r} must name its Arena member's bus")
    return ScenarioManifest(manifest_path, arena, duration_ns, referees, can_delays)
