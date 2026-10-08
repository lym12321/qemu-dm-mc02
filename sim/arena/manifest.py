"""Pure schema1 admission for two Robot records in the canonical shared world.

The explicit shared-world attachment retains each child's original Physics
declaration. This fixed profile only accepts the canonical four-wheel source
and two-chassis scene; compiling bindings and owning runtime resources are
separate consumer operations.
"""

from dataclasses import dataclass
from pathlib import Path

from sim.manifest import (ManifestError, fields, file_path, identifier,
                          load_document, text_bytes)
from sim.robots.manifest import (MAX_UPPER_COMPUTERS, Physics, RobotManifest,
                                load_manifest as load_robot_manifest)

_MODELS = Path(__file__).resolve().parents[2] / "models"
_PREFIXES = ("alpha/", "bravo/")


@dataclass(frozen=True)
class ArenaRobot:
    name: str
    prefix: str
    physics: str
    robot: RobotManifest


@dataclass(frozen=True)
class ArenaManifest:
    path: Path
    name: str
    world: Physics
    robots: tuple[ArenaRobot, ...]


def load_manifest(path: str | Path) -> ArenaManifest:
    """Resolve a fixed topology without acquiring simulation resources."""
    manifest_path, document = load_document(path)
    base = manifest_path.parent
    required = ("schema", "arena", "world", "robots")
    item = fields(document, required, required, "arena manifest")
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
    return ArenaManifest(manifest_path, name, world, tuple(result))
