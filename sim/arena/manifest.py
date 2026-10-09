"""Pure schema1 admission for two Robot records in the canonical shared world.

The explicit shared-world attachment retains each child's original Physics
declaration. This fixed profile only accepts the canonical four-wheel source
and two-chassis scene; compiling bindings and owning runtime resources are
separate consumer operations.
"""

from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING

from sim.manifest import (ManifestError, fields, file_path, identifier,
                          load_document, text_bytes)
from sim.robots.manifest import (MAX_UPPER_COMPUTERS, Physics, RobotManifest,
                                load_manifest as load_robot_manifest)

if TYPE_CHECKING:
    from sim.devices.referee import MatchDurations, RefereeState, ScriptEvent

_MODELS = Path(__file__).resolve().parents[2] / "models"
_PREFIXES = ("alpha/", "bravo/")


@dataclass(frozen=True)
class ArenaRobot:
    name: str
    prefix: str
    physics: str
    robot: RobotManifest


@dataclass(frozen=True)
class ArenaReferee:
    member: str
    uart: str
    initial: RefereeState
    durations: MatchDurations
    events: tuple[ScriptEvent, ...]
    cooling_42mm_per_s: int


@dataclass(frozen=True)
class ArenaManifest:
    path: Path
    name: str
    world: Physics
    robots: tuple[ArenaRobot, ...]
    referees: tuple[ArenaReferee, ...] = ()


def _typed_record(record_type, value, label):
    names = tuple(field.name for field in dataclass_fields(record_type))
    return record_type(**fields(value, names, names, label))


def parse_referee_events(value, label: str = "referee events") -> tuple[ScriptEvent, ...]:
    """Translate a bounded event list and validate it without runtime resources.

    Each entry uses reset-relative offset_ns and exactly one existing typed
    referee value. Preserve input order, including equal offsets. File loading
    and member/session admission belong to the caller.
    """
    from sim.devices.referee import (
        Buff, GameStatus, Hit, PowerHeat, RobotStatus, ScriptEvent, Shot,
        validate_referee_events,
    )
    from sim.devices.referee.engine import MAX_SCRIPT_EVENTS

    event_types = {"hit": Hit, "shot": Shot, "game": GameStatus,
                   "robot": RobotStatus, "power_heat": PowerHeat, "buff": Buff}
    if type(value) is not list or len(value) > MAX_SCRIPT_EVENTS:
        raise ManifestError(f"{label} must be a list with at most {MAX_SCRIPT_EVENTS} entries")
    events = []
    for index, configuration in enumerate(value):
        event_label = f"{label}[{index}]"
        event = fields(configuration, ("offset_ns", *event_types),
                       ("offset_ns",), event_label)
        kinds = [kind for kind in event_types if kind in event]
        if len(kinds) != 1:
            raise ManifestError(f"{event_label} must contain exactly one referee event value")
        kind = kinds[0]
        events.append(ScriptEvent(event["offset_ns"], _typed_record(
            event_types[kind], event[kind], event_label + "." + kind)))
    events = tuple(events)
    try:
        validate_referee_events(events)
    except (TypeError, ValueError) as error:
        raise ManifestError(f"{label}: {error}") from error
    return events


def _referees(value, members) -> dict[str, ArenaReferee]:
    if type(value) is not dict or len(value) > len(members):
        raise ManifestError("referees must be a mapping with at most one entry per Arena member")
    if not value:
        return {}

    # The old no-referee parser remains independent of optional domain imports.
    from sim.devices.referee import (
        Buff, GameStatus, MatchDurations, PowerHeat, RefereeState,
        RobotStatus, validate_referee_configuration,
    )

    groups = {"game": GameStatus, "robot": RobotStatus,
              "power_heat": PowerHeat, "buff": Buff}
    result = {}
    for alias, configuration in value.items():
        identifier(alias, "referees label")
        if alias not in members:
            raise ManifestError(f"referees.{alias} must name a declared Arena member")
        label = f"referees.{alias}"
        required = ("uart", "initial", "durations")
        item = fields(configuration, (*required, "events", "cooling_42mm_per_s"),
                      required, label)
        uart = item["uart"]
        text_bytes(uart, label + ".uart")
        if "\0" in uart:
            raise ManifestError(f"{label}.uart must not contain NUL")
        initial_fields = fields(item["initial"], groups, groups, label + ".initial")
        initial = RefereeState(**{
            name: _typed_record(record_type, initial_fields[name], f"{label}.initial.{name}")
            for name, record_type in groups.items()
        })
        durations = _typed_record(MatchDurations, item["durations"], label + ".durations")
        events = parse_referee_events(item.get("events", []), label + ".events")
        cooling = item.get("cooling_42mm_per_s", 0)
        try:
            validate_referee_configuration(initial, durations=durations, events=events,
                                           cooling_42mm_per_s=cooling)
        except (TypeError, ValueError) as error:
            raise ManifestError(f"{label}: {error}") from error
        result[alias] = ArenaReferee(alias, uart, initial, durations, events, cooling)
    return result


def load_manifest(path: str | Path) -> ArenaManifest:
    """Resolve a fixed topology without acquiring simulation resources."""
    manifest_path, document = load_document(path)
    base = manifest_path.parent
    required = ("schema", "arena", "world", "robots")
    item = fields(document, (*required, "referees"), required, "arena manifest")
    if type(item["schema"]) is not int or item["schema"] != 1:
        raise ManifestError("unsupported Arena schema; schema must be integer 1")
    arena = fields(item["arena"], ("name",), ("name",), "arena")
    name = identifier(arena["name"], "arena.name")
    specification = fields(item["world"], ("backend", "model"),
                           ("backend", "model"), "world")
    if type(specification["backend"]) is not str or specification["backend"] != "mujoco":
        raise ManifestError("world.backend must be mujoco")
    model = file_path(specification["model"], base, "world.model")
    if model != _MODELS / "two_four_wheel.xml":
        raise ManifestError("world.model must resolve to the canonical models/two_four_wheel.xml")
    world = Physics("mujoco", model)
    robots = item["robots"]
    if type(robots) is not dict or len(robots) != 2:
        raise ManifestError("robots must be a mapping containing exactly two entries")

    # Finish outer admission before reading either child YAML configuration.
    attachments = []
    for alias, configuration in robots.items():
        identifier(alias, "robots label")
        label = f"robots.{alias}"
        specification = fields(configuration, ("manifest", "prefix", "physics"),
                               ("manifest", "prefix", "physics"), label)
        prefix = specification["prefix"]
        if type(prefix) is not str or prefix not in _PREFIXES:
            raise ManifestError(f"{label}.prefix must be alpha/ or bravo/")
        selector = specification["physics"]
        if type(selector) is not str or selector != "shared-world":
            raise ManifestError(f"{label}.physics must be shared-world")
        text_bytes(specification["manifest"], label + ".manifest")
        attachments.append((prefix, alias, selector, specification["manifest"]))
    if {prefix for prefix, *_ in attachments} != set(_PREFIXES):
        raise ManifestError("robots must bind each of alpha/ and bravo/ exactly once")
    referees = _referees(item.get("referees", {}), robots)

    result = []
    upper_count = 0
    for prefix, alias, selector, reference in sorted(attachments):
        try:
            reference = file_path(reference, base, f"robots.{alias}.manifest")
            robot = load_robot_manifest(reference)
        except ManifestError as error:
            raise ManifestError(f"robots.{alias}.manifest {reference}: {error}") from error
        if robot.physics.model != _MODELS / "four_wheel.xml":
            raise ManifestError(f"robots.{alias}.manifest physics.model must resolve to "
                                "the canonical models/four_wheel.xml for shared-world")
        upper_count += len(robot.upper_computers)
        if upper_count > MAX_UPPER_COMPUTERS:
            raise ManifestError("Arena upper_computers total must not exceed 8 entries")
        result.append(ArenaRobot(alias, prefix, selector, robot))
    bound_referees = []
    for member in result:
        if member.name in referees:
            referee = referees[member.name]
            expected_uart = member.robot.board.name + ".uart7"
            if referee.uart != expected_uart:
                raise ManifestError(f"referees.{member.name}.uart must be {expected_uart}")
            bound_referees.append(referee)
    return ArenaManifest(manifest_path, name, world, tuple(result), tuple(bound_referees))
