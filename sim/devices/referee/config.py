"""Pure referee-domain configuration; no board, application or native owner."""

from collections.abc import Mapping
from dataclasses import fields as dataclass_fields

from sim.config.yaml import ManifestError, fields

from .engine import (
    MAX_SCRIPT_EVENTS, Hit, MatchDurations, RefereeState, ScriptEvent, Shot,
    validate_referee_configuration, validate_referee_events,
)
from .messages import Buff, GameStatus, PowerHeat, RobotStatus


def _mapping(value, allowed, required, label):
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} must be a mapping")
    return fields(dict(value), allowed, required, label)


def _record(record_type, value, label):
    names = tuple(field.name for field in dataclass_fields(record_type))
    return record_type(**_mapping(value, names, names, label))


def parse_referee_events(value, label="referee events"):
    """Admit bounded reset-relative events, preserving order on equal offsets."""
    kinds = {"hit": Hit, "shot": Shot, "game": GameStatus,
             "robot": RobotStatus, "power_heat": PowerHeat, "buff": Buff}
    if type(value) not in (list, tuple) or len(value) > MAX_SCRIPT_EVENTS:
        raise ManifestError(f"{label} must contain at most {MAX_SCRIPT_EVENTS} entries")
    result = []
    for index, configuration in enumerate(value):
        where = f"{label}[{index}]"
        item = _mapping(configuration, ("offset_ns", *kinds), ("offset_ns",), where)
        selected = [kind for kind in kinds if kind in item]
        if len(selected) != 1:
            raise ManifestError(f"{where} must contain exactly one referee event value")
        kind = selected[0]
        result.append(ScriptEvent(item["offset_ns"], _record(kinds[kind], item[kind], where + "." + kind)))
    result = tuple(result)
    try:
        validate_referee_events(result)
    except (TypeError, ValueError) as error:
        raise ManifestError(f"{label}: {error}") from error
    return result


def parse_referee_configuration(options, label="referee"):
    """Return existing engine constructor values without constructing an engine."""
    required = ("initial", "durations")
    item = _mapping(options, (*required, "events", "cooling_42mm_per_s"), required, label)
    groups = {"game": GameStatus, "robot": RobotStatus,
              "power_heat": PowerHeat, "buff": Buff}
    initial_values = _mapping(item["initial"], groups, groups, label + ".initial")
    initial = RefereeState(**{name: _record(kind, initial_values[name], label + ".initial." + name)
                              for name, kind in groups.items()})
    result = {"initial": initial,
              "durations": _record(MatchDurations, item["durations"], label + ".durations"),
              "events": parse_referee_events(item.get("events", ()), label + ".events"),
              "cooling_42mm_per_s": item.get("cooling_42mm_per_s", 0)}
    try:
        validate_referee_configuration(**result)
    except (TypeError, ValueError) as error:
        raise ManifestError(f"{label}: {error}") from error
    return result
