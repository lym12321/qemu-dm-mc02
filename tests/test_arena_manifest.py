"""Pure fixed two-robot admission, using the existing complete Robot records.

The firmware fixture is readable configuration input, not an executable ELF.
Native model composition and resource lifecycle are separate consumer gates.
"""

import builtins
from dataclasses import FrozenInstanceError, fields
import importlib
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.manifest import ManifestError
from sim.robots.manifest import Physics, RobotManifest, load_manifest as load_robot


ROOT = Path(__file__).resolve().parents[1]
ROBOT_MODEL = ROOT / "models" / "four_wheel.xml"
WORLD_MODEL = ROOT / "models" / "two_four_wheel.xml"
ROBOT_DOCUMENT = """schema: 1
robot: {name: four-wheel}
boards:
  main: {type: qemu, board: dm-mc02, firmware: firmware.elf}
buses:
  chassis_can: {type: can}
links:
  - {from: main.fdcan1, to: chassis_can}
devices:
  rr: {type: dji-m3508, bus: chassis_can, id: 4, joint: wheel_rr}
  fr: {type: dji-m3508, bus: chassis_can, id: 2, joint: wheel_fr}
  fl: {type: dji-m3508, bus: chassis_can, id: 1, joint: wheel_fl}
  rl: {type: dji-m3508, bus: chassis_can, id: 3, joint: wheel_rl}
physics: {backend: mujoco, model: MODEL}
""".replace("MODEL", str(ROBOT_MODEL))


def write_arena(directory, *, robot_documents=(ROBOT_DOCUMENT, ROBOT_DOCUMENT),
                members=None, name="two-four-wheel", world_model=None, referees=None):
    """Write only temporary configuration inputs; return the outer manifest.

    Native consumers may reuse this fixture and its canonical model references.
    Default YAML order and arbitrary aliases deliberately differ from prefixes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for prefix, document in zip(("alpha", "bravo"), robot_documents, strict=True):
        child = directory / "robots" / prefix
        child.mkdir(parents=True, exist_ok=True)
        (child / "firmware.elf").write_bytes(b"readability fixture; not an ELF image")
        (child / "robot.yaml").write_text(document, encoding="utf-8")
    if members is None:
        members = {
            "west": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/",
                     "physics": "shared-world"},
            "east": {"manifest": "robots/alpha/robot.yaml", "prefix": "alpha/",
                     "physics": "shared-world"},
        }
    document = {"schema": 1, "arena": {"name": name},
                "world": {"backend": "mujoco", "model": str(world_model or WORLD_MODEL)},
                "robots": members}
    if referees is not None:
        document["referees"] = referees
    path = directory / "arena.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def load_manifest(path):
    # Keep importing this test fixture usable by native consumers without
    # importing Arena or any runtime during collection.
    from sim.arena.manifest import load_manifest as load
    return load(path)


def forbid_child_reads(directory, monkeypatch):
    child_paths = {(directory / "robots" / prefix / "robot.yaml").resolve()
                   for prefix in ("alpha", "bravo")}
    original_open = Path.open

    def open_file(self, *args, **kwargs):
        if self.resolve() in child_paths:
            pytest.fail("outer rejection read a child before complete admission")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)


def write_executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path.resolve()


def test_import_and_success_failure_never_acquire_runtime_resources(tmp_path, monkeypatch):
    computers = "upper_computers: {vision: {command: ['./node']}}\n"
    path = write_arena(tmp_path, robot_documents=(ROBOT_DOCUMENT + computers,) * 2)
    for prefix in ("alpha", "bravo"):
        write_executable(tmp_path / "robots" / prefix / "node")

    def forbidden(*args, **kwargs):
        pytest.fail("Arena admission acquired a process or socket")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    original_import = builtins.__import__
    forbidden_modules = ("mujoco", "numpy", "sim.physics", "sim.fabric", "sim.devices",
                         "sim.boards", "sim.lab.runtime", "sim.robots.runtime",
                         "sim.robots.four_wheel", "sim.robots.shared_world", "tools")

    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".")
                   for prefix in forbidden_modules):
                pytest.fail(f"Arena admission imported a runtime dependency: {candidate}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", pure_import)
    importlib.import_module("sim.arena")
    # Execute source imports without replacing the canonical record classes
    # already borrowed by runtime consumers collected in the same process.
    spec = importlib.util.spec_from_file_location("arena_manifest_purity", ROOT / "sim/arena/manifest.py")
    arena = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, arena)
    spec.loader.exec_module(arena)
    result = arena.load_manifest(path)
    assert sum(len(member.robot.upper_computers) for member in result.robots) == 2
    assert result.referees == ()
    document = yaml.safe_load(path.read_text())
    document["referees"] = {}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert arena.load_manifest(path).referees == ()
    child = tmp_path / "robots" / "alpha" / "robot.yaml"
    child.write_text(ROBOT_DOCUMENT + "unexpected: true\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        arena.load_manifest(path)


def test_complete_typed_frozen_records_preserve_aliases_and_canonical_order(tmp_path):
    path = write_arena(tmp_path)
    result = load_manifest(path)
    from sim.arena.manifest import ArenaManifest, ArenaRobot

    assert isinstance(result, ArenaManifest)
    assert result.referees == ()
    assert (result.path, result.name, result.world) == (
        path.resolve(), "two-four-wheel", Physics("mujoco", WORLD_MODEL.resolve()))
    assert type(result.robots) is tuple
    assert [(member.name, member.prefix, member.physics) for member in result.robots] == [
        ("east", "alpha/", "shared-world"), ("west", "bravo/", "shared-world")]
    for member in result.robots:
        child = tmp_path / "robots" / member.prefix[:-1] / "robot.yaml"
        assert isinstance(member, ArenaRobot) and isinstance(member.robot, RobotManifest)
        assert member.robot == load_robot(child)
        assert (member.robot.board.name, member.robot.bus.name, member.robot.physics.model) == (
            "main", "chassis_can", ROBOT_MODEL.resolve())
        assert [(motor.motor_id, motor.joint) for motor in member.robot.devices] == [
            (1, "wheel_fl"), (2, "wheel_fr"), (3, "wheel_rl"), (4, "wheel_rr")]
    scoped_ids = {(member.name, motor.motor_id)
                  for member in result.robots for motor in member.robot.devices}
    assert scoped_ids == {(name, motor_id) for name in ("east", "west") for motor_id in (1, 2, 3, 4)}
    assert [member.prefix + motor.joint for member in result.robots for motor in member.robot.devices] == [
        "alpha/wheel_fl", "alpha/wheel_fr", "alpha/wheel_rl", "alpha/wheel_rr",
        "bravo/wheel_fl", "bravo/wheel_fr", "bravo/wheel_rl", "bravo/wheel_rr"]
    for record in (result, result.world, *result.robots):
        attribute = fields(record)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, getattr(record, attribute))
    document = yaml.safe_load(path.read_text())
    document["robots"] = dict(reversed(tuple(document["robots"].items())))
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert load_manifest(path) == result


def test_absent_and_actual_uart_ready_bytes_stay_in_child_scope(tmp_path):
    documents = (ROBOT_DOCUMENT,
                 ROBOT_DOCUMENT + 'ready: {endpoint: main.uart7, text: "é\\u0000READY"}\n')
    result = load_manifest(write_arena(tmp_path, robot_documents=documents))
    assert result.robots[0].robot.ready is None
    ready = result.robots[1].robot.ready
    assert (ready.endpoint, ready.text) == ("main.uart7", b"\xc3\xa9\0READY")


def test_symlink_sources_resolve_child_cwd_and_keep_literal_arguments(tmp_path, monkeypatch):
    source = tmp_path / "child-source"
    source.mkdir()
    target = write_executable(source / "bin" / "vision")
    (source / "firmware.elf").write_bytes(b"explicit readability fixture")
    (source / "single.xml").symlink_to(ROBOT_MODEL)
    arguments = ["vision", "", "two words", "$HOME", "$(touch unexpected)", "`touch unexpected`", "*"]
    child = source / "robot.yaml"
    child.write_text(ROBOT_DOCUMENT.replace(str(ROBOT_MODEL), "single.xml") +
                     yaml.safe_dump({"upper_computers": {"vision": {"command": arguments}}}),
                     encoding="utf-8")
    directory = tmp_path / "arena-source"
    members = {
        "west": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/", "physics": "shared-world"},
        "east": {"manifest": "alpha.yaml", "prefix": "alpha/", "physics": "shared-world"},
    }
    path = write_arena(directory, members=members, world_model="scene.xml")
    (directory / "scene.xml").symlink_to(WORLD_MODEL)
    (directory / "alpha.yaml").symlink_to(child)
    alias = tmp_path / "arena-alias.yaml"
    alias.symlink_to(path)
    outside = tmp_path / "unrelated-cwd"
    write_executable(outside / "bin" / "vision")
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PATH", "bin")
    result = load_manifest(alias)
    robot = result.robots[0].robot
    assert result.path == path.resolve() and result.world.model == WORLD_MODEL.resolve()
    assert (robot.path, robot.board.firmware, robot.physics.model) == (
        child.resolve(), source / "firmware.elf", ROBOT_MODEL.resolve())
    upper = robot.upper_computers[0]
    assert (upper.name, upper.cwd, upper.command) == ("vision", source, (str(target), *arguments[1:]))
    assert robot.ready is None
    assert not (source / "unexpected").exists() and not (outside / "unexpected").exists()


DELETE = object()


@pytest.mark.parametrize("location,replacement", [
    (("schema",), True),
    (("scenario",), {}),
    (("arena", "name"), "场地"),
    (("robots",), {"only": {}}),
    (("robots", "east", "extra"), True),
    (("robots", "east", "prefix"), "bravo/"),
    (("robots", "east", "prefix"), "charlie/"),
    (("robots", "east", "prefix"), ["alpha/"]),
    (("robots", "east", "physics"), DELETE),
    (("robots", "east", "physics"), "robot-world"),
    (("robots", "east", "physics"), {"source": "shared-world"}),
    (("robots", "west", "manifest"), {"schema": 1}),
    (("world", "backend"), "gazebo"),
], ids=["bool-schema", "unknown-root", "non-ascii-name", "one-member",
        "unknown-last-member-field", "duplicate-prefix", "unknown-prefix", "unhashable-prefix",
        "missing-shared-world", "wrong-selector", "unhashable-selector", "inline-reference", "world-backend"])
def test_complete_outer_admission_precedes_any_child_file_open(tmp_path, monkeypatch, location, replacement):
    path = write_arena(tmp_path)
    document = yaml.safe_load(path.read_text())
    item = document
    for key in location[:-1]:
        item = item[key]
    if replacement is DELETE:
        del item[location[-1]]
    else:
        item[location[-1]] = replacement
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_member_alias_uses_ascii_identifier_instead_of_prefix_policy(tmp_path):
    members = {
        "A_" + "x" * 62: {"manifest": "robots/alpha/robot.yaml", "prefix": "alpha/", "physics": "shared-world"},
        "B-two": {"manifest": "robots/bravo/robot.yaml", "prefix": "bravo/", "physics": "shared-world"},
    }
    path = write_arena(tmp_path, members=members, name="Arena_1")
    assert [member.name for member in load_manifest(path).robots] == ["A_" + "x" * 62, "B-two"]
    members["máin"] = members.pop("B-two")
    with pytest.raises(ManifestError, match="ASCII"):
        load_manifest(write_arena(tmp_path, members=members))


@pytest.mark.parametrize("change", ["duplicate", "anchor", "unsafe-tag"])
def test_outer_yaml_uses_shared_safe_unique_loader(tmp_path, change):
    path = write_arena(tmp_path)
    document = path.read_text()
    if change == "duplicate":
        document = "schema: 1\n" + document
    elif change == "anchor":
        document = document.replace("arena:", "arena: &named", 1)
    else:
        document = document.replace("arena:\n  name: two-four-wheel\n",
                                    "arena: !!python/object:builtins.object {}\n", 1)
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_byte_identical_world_copy_is_outside_fixed_profile(tmp_path):
    copied = tmp_path / "copied-world.xml"
    copied.write_bytes(WORLD_MODEL.read_bytes())
    with pytest.raises(ManifestError):
        load_manifest(write_arena(tmp_path, world_model=copied))


def test_identical_custom_child_models_do_not_implicitly_replace_source(tmp_path):
    copied = tmp_path / "copied-robot.xml"
    copied.write_bytes(ROBOT_MODEL.read_bytes())
    custom = ROBOT_DOCUMENT.replace(str(ROBOT_MODEL), str(copied))
    with pytest.raises(ManifestError):
        load_manifest(write_arena(tmp_path, robot_documents=(custom, custom)))


@pytest.mark.parametrize("counts,same_reference,accepted", [
    ((4, 4), True, True),
    ((4, 5), False, False),
    ((5, 5), True, False),
], ids=["repeated-four-is-eight", "different-four-plus-five-is-nine", "repeated-five-is-ten"])
def test_upper_budget_counts_each_member_even_for_repeated_references(tmp_path, counts, same_reference, accepted):
    documents = tuple(ROBOT_DOCUMENT + yaml.safe_dump({"upper_computers": {
        f"node_{index}": {"command": ["./node"]} for index in range(count)}}) for count in counts)
    path = write_arena(tmp_path, robot_documents=documents)
    for prefix in ("alpha", "bravo"):
        write_executable(tmp_path / "robots" / prefix / "node")
    if same_reference:
        document = yaml.safe_load(path.read_text())
        document["robots"]["west"]["manifest"] = "robots/alpha/robot.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    if accepted:
        result = load_manifest(path)
        assert sum(len(member.robot.upper_computers) for member in result.robots) == 8
        assert result.robots[0].robot == result.robots[1].robot
        assert result.robots[0].name != result.robots[1].name
    else:
        with pytest.raises(ManifestError, match="8"):
            load_manifest(path)


@pytest.mark.parametrize("child_is_arena", [False, True], ids=["malformed-robot", "arena-not-robot"])
def test_child_error_keeps_member_reference_context_and_original_cause(tmp_path, child_is_arena):
    path = write_arena(tmp_path)
    child = tmp_path / "robots" / "alpha" / "robot.yaml"
    child.write_text(path.read_text() if child_is_arena else ROBOT_DOCUMENT + "unexpected: true\n",
                     encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_manifest(path)
    assert "east" in str(caught.value) and str(child.resolve()) in str(caught.value)
    assert isinstance(caught.value.__cause__, ManifestError)
    assert "unknown" in str(caught.value.__cause__)


def test_reference_rejects_nul_before_filesystem_use(tmp_path):
    path = write_arena(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["robots"]["east"]["manifest"] = "robots/alpha/robot\0.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ManifestError, match="NUL"):
        load_manifest(path)


def referee_configuration(*, uart="main.uart7", scripted=False):
    """Explicit protocol values; no engine, owner or rule defaults in this fixture."""
    initial = {
        "game": {"game_type": 1, "game_progress": 2, "stage_remain_time": 1,
                 "sync_timestamp": 0x0102030405060708},
        "robot": {"robot_id": 3, "robot_level": 2, "current_hp": 100,
                  "maximum_hp": 200, "cooling_per_s": 3, "heat_limit": 100,
                  "chassis_power_limit": 80, "power_outputs": 5},
        "power_heat": {"buffer_energy_j": 40, "heat_17mm": 10, "heat_42mm": 20},
        "buff": {"recovery_percent": 5, "cooling_per_s": 9, "defence_percent": 7,
                 "vulnerability_percent": 11, "attack_percent": 13,
                 "remaining_energy": 254},
    }
    result = {"uart": uart, "initial": initial,
              "durations": {"countdown_s": 3, "battle_s": 4}}
    if scripted:
        result["cooling_42mm_per_s"] = 2
        result["events"] = [
            {"offset_ns": 0, "hit": {"damage_hp": 20}},
            {"offset_ns": 0, "robot": initial["robot"].copy()},
            {"offset_ns": 0, "power_heat": initial["power_heat"].copy()},
            {"offset_ns": 500_000_000, "shot": {"heat_17mm": 5, "heat_42mm": 7}},
            {"offset_ns": 500_000_000, "hit": {"damage_hp": 30}},
            {"offset_ns": 1_000_000_000,
             "game": {**initial["game"], "game_progress": 3, "stage_remain_time": 3}},
            {"offset_ns": 1_000_000_000,
             "robot": {**initial["robot"], "current_hp": 70,
                       "chassis_power_limit": 120, "power_outputs": 6}},
            {"offset_ns": 1_000_000_000,
             "buff": {"recovery_percent": 8, "cooling_per_s": 17, "defence_percent": 19,
                      "vulnerability_percent": 23, "attack_percent": 29,
                      "remaining_energy": 128}},
        ]
    return result


def test_public_referee_event_parser_is_pure_typed_frozen_and_preserves_input_order(monkeypatch):
    from sim.arena.manifest import parse_referee_events
    from sim.core import Coordinator
    from sim.devices.referee import (
        Buff, GameStatus, Hit, PowerHeat, RefereeEngine, RefereeUartEndpoint,
        RobotStatus, ScriptEvent, Shot, validate_referee_events,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("pure event admission acquired or executed a runtime owner")
    for owner in (RefereeEngine, RefereeUartEndpoint, Coordinator):
        monkeypatch.setattr(owner, "__init__", forbidden)
    for method in ("reset", "step", "queue_events", "get_state", "close"):
        monkeypatch.setattr(RefereeEngine, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    robot = RobotStatus(3, 2, 100, 200, 3, 100, 80, 5)
    expected = (
        ScriptEvent(0, Hit(20)), ScriptEvent(0, robot), ScriptEvent(0, PowerHeat(40, 10, 20)),
        ScriptEvent(500_000_000, Shot(5, 7)), ScriptEvent(500_000_000, Hit(30)),
        ScriptEvent(1_000_000_000, GameStatus(1, 3, 3, 0x0102030405060708)),
        ScriptEvent(1_000_000_000, RobotStatus(3, 2, 70, 200, 3, 100, 120, 6)),
        ScriptEvent(1_000_000_000, Buff(8, 17, 19, 23, 29, 128)),
    )
    calls = []
    def validate(events):
        calls.append(events)
        return validate_referee_events(events)
    from sim.devices import referee as referee_module
    monkeypatch.setattr(referee_module, "validate_referee_events", validate)
    raw = referee_configuration(scripted=True)["events"]
    result = parse_referee_events(raw)
    assert type(result) is tuple and result == expected
    assert calls == [result] and calls[0] is result
    assert validate_referee_events(expected) is None
    for event in result:
        with pytest.raises(FrozenInstanceError):
            event.offset_ns = 99
        attribute = fields(event.value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(event.value, attribute, 99)
    raw[0]["hit"]["damage_hp"] = 99
    raw.reverse()
    assert result == expected
    assert parse_referee_events([]) == ()


def test_referee_records_are_scoped_frozen_and_canonical_without_resource_acquisition(
        tmp_path, monkeypatch):
    from sim.arena.manifest import ArenaReferee
    from sim.core import Coordinator
    from sim.devices.referee import RefereeEngine, RefereeUartEndpoint

    def forbidden(*args, **kwargs):
        pytest.fail("Arena referee admission constructed or executed a runtime owner")

    for owner in (RefereeEngine, RefereeUartEndpoint, Coordinator):
        monkeypatch.setattr(owner, "__init__", forbidden)
    for method in ("reset", "step", "get_state", "close"):
        monkeypatch.setattr(RefereeEngine, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_import = builtins.__import__

    def pure_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = (importlib.util.resolve_name("." * level + name, globals["__package__"])
                    if level else name)
        candidates = (resolved, *(resolved + "." + item for item in (fromlist or ()) if item != "*"))
        for candidate in candidates:
            if any(candidate == prefix or candidate.startswith(prefix + ".") for prefix in (
                    "mujoco", "numpy", "sim.physics", "sim.arena.runtime", "sim.robots.runtime",
                    "sim.robots.shared_world", "sim.lab.runtime", "tools")):
                pytest.fail(f"Arena referee admission imported a runtime backend: {candidate}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", pure_import)
    from sim.arena import manifest as arena_manifest
    translate = arena_manifest.parse_referee_events
    translated = []
    def parse_events(value, label="referee events"):
        result = translate(value, label)
        translated.append((label, result))
        return result
    monkeypatch.setattr(arena_manifest, "parse_referee_events", parse_events)
    path = write_arena(tmp_path, referees={"west": referee_configuration(),
                                            "east": referee_configuration(scripted=True)})
    result = load_manifest(path)
    assert type(result.referees) is tuple
    east, west = result.referees
    assert len(translated) == 2
    assert dict(translated) == {"referees.east.events": east.events, "referees.west.events": west.events}
    assert all(isinstance(item, ArenaReferee) for item in result.referees)
    assert [(item.member, item.uart) for item in result.referees] == [
        ("east", "main.uart7"), ("west", "main.uart7")]
    assert (west.events, west.cooling_42mm_per_s) == ((), 0)
    assert east.initial == west.initial and east.cooling_42mm_per_s == 2
    assert [type(event.value).__name__ for event in east.events] == [
        "Hit", "RobotStatus", "PowerHeat", "Shot", "Hit", "GameStatus", "RobotStatus", "Buff"]
    assert [event.offset_ns for event in east.events] == [0, 0, 0, 500_000_000, 500_000_000,
                                                          1_000_000_000, 1_000_000_000, 1_000_000_000]
    for record in (east, east.initial, east.initial.game, east.initial.robot,
                   east.initial.power_heat, east.initial.buff, east.durations,
                   *east.events, *(event.value for event in east.events)):
        attribute = fields(record)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(record, attribute, getattr(record, attribute))
    document = yaml.safe_load(path.read_text())
    document["referees"] = dict(reversed(tuple(document["referees"].items())))
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert load_manifest(path) == result


EVENT_REJECTIONS = (
    ({}, "non-list-events"),
    ([{"offset_ns": 0}], "no-event-value"),
    ([{"offset_ns": 0, "hit": {"damage_hp": 1},
                         "shot": {"heat_17mm": 1, "heat_42mm": 0}}], "two-event-values"),
    ([{"offset_ns": 0, "damage": {"damage_hp": 1}}], "unknown-event"),
    ([{"offset_ns": True, "hit": {"damage_hp": 0}}], "bool-offset"),
    ([{"offset_ns": 1 << 64, "hit": {"damage_hp": 0}}], "offset-overflow"),
    ([{"offset_ns": 1, "hit": {"damage_hp": 0}},
      {"offset_ns": 0, "hit": {"damage_hp": 0}}], "unsorted-offsets"),
    ([{"offset_ns": 0, "hit": {"damage_hp": True}}], "bool-hit"),
    ([{"offset_ns": 0, "shot": {"heat_17mm": 0}}], "partial-shot"),
    ([{"offset_ns": 0, "shot": {"heat_17mm": 0, "heat_42mm": 65536}}], "shot-overflow"),
)


@pytest.mark.parametrize("value", [pytest.param(value, id=ident) for value, ident in EVENT_REJECTIONS
                                  if ident in ("non-list-events", "two-event-values", "unsorted-offsets",
                                               "partial-shot", "shot-overflow")])
def test_public_referee_event_parser_reuses_contextual_shape_range_and_order_rejection(value):
    from sim.arena.manifest import parse_referee_events
    with pytest.raises(ManifestError, match=r"scenario\.script"):
        parse_referee_events(value, "scenario.script")


@pytest.mark.parametrize("location,replacement", [
    (("referees",), None),
    (("referees",), []),
    (("referees",), {"unknown": {}}),
    (("referees", "east"), []),
    (("referees", "east", "period_ns"), 10),
    (("referees", "east", "uart"), True),
    (("referees", "east", "uart"), "main\0.uart7"),
    (("referees", "east", "initial", "buff"), DELETE),
    (("referees", "east", "initial", "game", "sync_timestamp"), DELETE),
    (("referees", "east", "initial", "robot", "automatic_revive"), False),
    (("referees", "east", "initial", "power_heat", "heat_17mm"), True),
    (("referees", "east", "initial", "buff", "remaining_energy"), 256),
    (("referees", "east", "initial", "game", "sync_timestamp"), 1 << 64),
    (("referees", "east", "initial", "robot", "current_hp"), 201),
    (("referees", "east", "initial", "game", "stage_remain_time"), 0),
    (("referees", "east", "durations", "countdown_s"), 0),
    (("referees", "east", "durations", "battle_s"), True),
    (("referees", "east", "cooling_42mm_per_s"), -1),
    *((("referees", "east", "events"), value) for value, _ in EVENT_REJECTIONS),
], ids=[
        "null-table", "non-mapping-table", "unbound-alias", "non-mapping-entry", "unknown-policy",
        "bool-uart", "nul-uart", "missing-initial-group", "missing-explicit-timestamp",
        "unknown-message-field", "bool-wire-field", "uint8-overflow", "timestamp-overflow",
        "hp-policy", "active-phase-zero", "zero-duration", "bool-duration", "negative-cooling",
        *(ident for _, ident in EVENT_REJECTIONS)])
def test_referee_outer_shape_and_domain_reject_before_any_child_read(
        tmp_path, monkeypatch, location, replacement):
    path = write_arena(tmp_path, referees={"west": referee_configuration(),
                                            "east": referee_configuration()})
    document = yaml.safe_load(path.read_text())
    item = document
    for key in location[:-1]:
        item = item[key]
    if replacement is DELETE:
        del item[location[-1]]
    else:
        item[location[-1]] = replacement
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    forbid_child_reads(tmp_path, monkeypatch)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("count,accepted", [(1024, True), (1025, False)])
def test_referee_script_capacity_and_maximum_offset(tmp_path, monkeypatch, count, accepted):
    from sim.arena.manifest import parse_referee_events
    configuration = referee_configuration()
    configuration["events"] = [{"offset_ns": 0, "hit": {"damage_hp": 0}}
                               for _ in range(count - 1)]
    configuration["events"].append({"offset_ns": 0xffffffffffffffff, "hit": {"damage_hp": 1}})
    path = write_arena(tmp_path, referees={"east": configuration})
    if accepted:
        events = parse_referee_events(configuration["events"])
        assert len(events) == 1024 and events[-1].offset_ns == 0xffffffffffffffff
        record, = load_manifest(path).referees
        assert len(record.events) == 1024 and record.events[-1].offset_ns == 0xffffffffffffffff
    else:
        with pytest.raises(ManifestError, match="referee events.*1024"):
            parse_referee_events(configuration["events"])
        forbid_child_reads(tmp_path, monkeypatch)
        with pytest.raises(ManifestError, match="1024"):
            load_manifest(path)


@pytest.mark.parametrize("uart", ["main.uart7", "bravo_cpu.uart7", "alpha_cpu.uart1"])
def test_referee_uart_association_uses_its_loaded_child_board(tmp_path, uart):
    documents = tuple(ROBOT_DOCUMENT.replace("main", name) for name in ("alpha_cpu", "bravo_cpu"))
    path = write_arena(tmp_path, robot_documents=documents,
                       referees={"east": referee_configuration(uart=uart)})
    with pytest.raises(ManifestError, match="uart7"):
        load_manifest(path)
    path = write_arena(tmp_path, robot_documents=documents,
                       referees={"west": referee_configuration(uart="bravo_cpu.uart7"),
                                 "east": referee_configuration(uart="alpha_cpu.uart7")})
    assert [(record.member, record.uart) for record in load_manifest(path).referees] == [
        ("east", "alpha_cpu.uart7"), ("west", "bravo_cpu.uart7")]


@pytest.mark.parametrize("public_events", [False, True], ids=["arena", "public_parser"])
def test_admitted_referees_feed_real_coordinator_with_literal_wire_and_reset_origins(tmp_path, public_events):
    from sim.arena.manifest import parse_referee_events
    from sim.core import Coordinator, Session, StepResult
    from sim.devices.referee import RefereeEngine, encode_message

    admitted = load_manifest(write_arena(tmp_path, referees={"west": referee_configuration(),
                                                              "east": referee_configuration(scripted=True)}))
    scripts = {record.member: (parse_referee_events(
        referee_configuration(scripted=record.member == "east").get("events", []))
        if public_events else record.events) for record in admitted.referees}
    engines = {record.member: RefereeEngine(record.initial, durations=record.durations,
                                           events=scripts[record.member],
                                           cooling_42mm_per_s=record.cooling_42mm_per_s)
               for record in admitted.referees}
    coordinator = Coordinator(engines)
    # Official 2026V1.1.0 layouts; fixed CRC bytes calculated independently with
    # pinned crccheck, as in the existing actual RefereeTest direct consumer.
    literals = tuple(bytes.fromhex(value) for value in (
        "a50b000002010021010008070605040302017d07"
        "a50d00018d010203026400c80003006400500005ef8a"
        "a50e00028b0202000000000000000028000a001400c1da"
        "a5080003040402050900070b0d00feec0b",
        "a50b00046301003103000807060504030201f59e"
        "a50d0005ec010203024600c800030064007800060a27"
        "a50e0006ea0202000000000000000028000c001900c5f8"
        "a508000765040208110013171d0080d577",
    ))

    def batch(state, sequence):
        return b"".join(encode_message(value, sequence + index) for index, value in enumerate(
            (state.game, state.robot, state.power_heat, state.buff)))

    try:
        for session_id, origin in ((7, 12_345_678), (8, 50_000_000_000)):
            coordinator.reset(Session(session_id, origin))
            assert batch(engines["east"].get_state(), 0) == literals[0]
            assert engines["east"].get_state().robot.current_hp == 100  # hit precedes replacement
            assert coordinator.step(1_000_000_000) == {
                "east": StepResult("ok", origin + 1_000_000_000),
                "west": StepResult("ok", origin + 1_000_000_000)}
            assert batch(engines["east"].get_state(), 4) == literals[1]
            west = engines["west"].get_state()
            assert (west.robot.current_hp, west.power_heat.heat_17mm, west.power_heat.heat_42mm) == (
                100, 7, 20)
            assert coordinator.t_sim_ns == engines["east"].t_sim_ns == engines["west"].t_sim_ns == (
                origin + 1_000_000_000)
    finally:
        coordinator.close()
    assert coordinator.closed and all(engine.closed for engine in engines.values())
