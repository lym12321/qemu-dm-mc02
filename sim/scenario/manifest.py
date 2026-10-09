"""Pure Scenario admission for explicit scoped referee events.

Event times are relative to the shared host reset. Duration is the Arena active
duration after requested ready markers; startup events may precede that origin.
The existing engine owns event execution. This module reads configuration only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sim.arena.manifest import (
    ArenaManifest, load_manifest as load_arena, parse_referee_events,
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
class ScenarioManifest:
    path: Path
    arena: ArenaManifest
    duration_ns: int
    referees: tuple[ScenarioReferee, ...]


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
    item = fields(document, (*required, "referee_events"), required, "scenario manifest")
    if type(item["schema"]) is not int or item["schema"] != 1:
        raise ManifestError("unsupported Scenario schema; schema must be integer 1")
    duration_ns = parse_time(item["duration"], "duration", zero=False)
    events = _referee_events(item.get("referee_events", {}), duration_ns)
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
    return ScenarioManifest(manifest_path, arena, duration_ns, referees)
