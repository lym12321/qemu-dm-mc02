"""Pure schema 1 admission for the fixed single-board four-wheel profile.

Firmware/model paths and upper-computer working directories belong to the
resolved manifest directory. Commands retain literal arguments and resolve
their executable once; admission starts no process, transport or physics.
Native model/binding validation remains the worker's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from sim.config.yaml import (ManifestError, fields, file_path as _file,
                          identifier as _label, load_document, text_bytes)


MAX_UPPER_COMPUTERS = 8
MAX_ARGUMENTS = 128
MAX_ARGUMENT_BYTES = 4096
MAX_COMMAND_BYTES = 16 * 1024
_JOINTS = {1: "wheel_fl", 2: "wheel_fr", 3: "wheel_rl", 4: "wheel_rr"}
_UARTS = ("uart1", "uart2", "uart3", "uart5", "uart7", "uart10")


@dataclass(frozen=True)
class Board:
    name: str
    type: str
    board: str
    firmware: Path


@dataclass(frozen=True)
class CanBus:
    name: str
    type: str


@dataclass(frozen=True)
class Link:
    source: str
    target: str


@dataclass(frozen=True)
class Motor:
    name: str
    type: str
    bus: str
    motor_id: int
    joint: str


@dataclass(frozen=True)
class Physics:
    backend: str
    model: Path


@dataclass(frozen=True)
class UpperComputer:
    name: str
    command: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class Ready:
    """An actual UART marker requested by the caller, never inferred readiness."""

    endpoint: str
    text: bytes


@dataclass(frozen=True)
class RobotManifest:
    path: Path
    name: str
    board: Board
    bus: CanBus
    link: Link
    devices: tuple[Motor, ...]
    physics: Physics
    upper_computers: tuple[UpperComputer, ...]
    ready: Ready | None = None


def _literal(value, expected: str, label: str) -> str:
    if type(value) is not str or value != expected:
        raise ManifestError(f"{label} must be {expected}")
    return value


def _single(value, label: str) -> tuple[str, object]:
    if type(value) is not dict or len(value) != 1:
        raise ManifestError(f"{label} must be a mapping containing exactly one entry")
    name, configuration = next(iter(value.items()))
    return _label(name, label + " label"), configuration


def _string(value, label: str, *, empty=False, maximum=MAX_ARGUMENT_BYTES) -> bytes:
    encoded = text_bytes(value, label, empty=empty, maximum=maximum)
    if "\0" in value:
        raise ManifestError(f"{label} must not contain NUL")
    return encoded


def _executable(value: str, cwd: Path, label: str) -> Path:
    try:
        if "/" in value:
            candidate = cwd / value
        else:
            # Popen's POSIX child searches relative/empty PATH entries after
            # changing cwd. Normalize only those directories, then let the
            # standard library perform the native executable search.
            search = os.pathsep.join(str(cwd / directory)
                                     for directory in os.get_exec_path())
            found = shutil.which(value, path=search)
            if found is None:
                raise ManifestError(f"{label} executable is not found in PATH: {value}")
            candidate = Path(found)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ManifestError(f"{label} executable must be an executable regular file")
        return resolved
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError(f"{label} executable cannot be resolved: {error}") from error


def _upper_computers(value, cwd: Path) -> tuple[UpperComputer, ...]:
    if type(value) is not dict or len(value) > MAX_UPPER_COMPUTERS:
        raise ManifestError("upper_computers must be a mapping with at most 8 entries")
    result = []
    for name, configuration in value.items():
        _label(name, "upper_computers label")
        label = f"upper_computers.{name}"
        item = fields(configuration, ("command",), ("command",), label)
        command = item["command"]
        if type(command) is not list or not 1 <= len(command) <= MAX_ARGUMENTS:
            raise ManifestError(f"{label}.command must contain 1..128 string arguments")
        for index, argument in enumerate(command):
            _string(argument, f"{label}.command[{index}]", empty=index > 0)
        if not command[0].strip():
            raise ManifestError(f"{label}.command executable must not be blank")
        executable = _executable(command[0], cwd, label)
        arguments = (str(executable), *command[1:])
        size = sum(len(_string(argument, f"{label}.command[{index}]", empty=index > 0)) + 1
                   for index, argument in enumerate(arguments))
        if size > MAX_COMMAND_BYTES:
            raise ManifestError(f"{label}.command exceeds 16 KiB including argument terminators")
        result.append(UpperComputer(name, arguments, cwd))
    return tuple(result)


def load_manifest(path: str | Path) -> RobotManifest:
    """Read and admit one fixed robot, preserving labels and canonical motor IDs."""
    manifest_path, document = load_document(path)
    base = manifest_path.parent
    required = ("schema", "robot", "boards", "buses", "links", "devices", "physics")
    item = fields(document, (*required, "upper_computers", "ready"), required, "robot manifest")
    if type(item["schema"]) is not int or item["schema"] != 1:
        raise ManifestError("unsupported Robot schema; schema must be integer 1")
    robot = fields(item["robot"], ("name",), ("name",), "robot")
    _string(robot["name"], "robot.name", maximum=256)
    if not robot["name"].strip():
        raise ManifestError("robot.name must not be blank")

    board_name, configuration = _single(item["boards"], "boards")
    specification = fields(configuration, ("type", "board", "firmware"),
                           ("type", "board", "firmware"), f"boards.{board_name}")
    board_type = _literal(specification["type"], "qemu", "board type")
    board_model = _literal(specification["board"], "dm-mc02", "board")
    board = Board(board_name, board_type, board_model,
                  _file(specification["firmware"], base, "firmware"))

    bus_name, configuration = _single(item["buses"], "buses")
    specification = fields(configuration, ("type",), ("type",), f"buses.{bus_name}")
    bus = CanBus(bus_name, _literal(specification["type"], "can", "bus type"))

    links = item["links"]
    if type(links) is not list or len(links) != 1:
        raise ManifestError("links must be a list containing exactly one entry")
    specification = fields(links[0], ("from", "to"), ("from", "to"), "links[0]")
    link = Link(_literal(specification["from"], board_name + ".fdcan1", "links[0].from"),
                _literal(specification["to"], bus_name, "links[0].to"))

    devices = item["devices"]
    if type(devices) is not dict or len(devices) != 4:
        raise ManifestError("devices must contain exactly the four M3508 motors")
    motors = []
    identifiers = set()
    for name, configuration in devices.items():
        _label(name, "devices label")
        label = f"devices.{name}"
        specification = fields(configuration, ("type", "bus", "id", "joint"),
                               ("type", "bus", "id", "joint"), label)
        motor_type = _literal(specification["type"], "dji-m3508", label + ".type")
        motor_bus = _literal(specification["bus"], bus_name, label + ".bus")
        motor_id = specification["id"]
        if type(motor_id) is not int or motor_id not in _JOINTS:
            raise ManifestError(f"{label}.id must be integer 1, 2, 3 or 4")
        if motor_id in identifiers:
            raise ManifestError(f"{label}.id repeats motor ID {motor_id}")
        identifiers.add(motor_id)
        joint = _literal(specification["joint"], _JOINTS[motor_id], label + ".joint")
        motors.append(Motor(name, motor_type, motor_bus, motor_id, joint))

    specification = fields(item["physics"], ("backend", "model"),
                           ("backend", "model"), "physics")
    physics = Physics(_literal(specification["backend"], "mujoco", "physics.backend"),
                      _file(specification["model"], base, "physics.model"))
    computers = _upper_computers(item.get("upper_computers", {}), base)
    ready = None
    if "ready" in item:
        specification = fields(item["ready"], ("endpoint", "text"), ("endpoint", "text"), "ready")
        endpoint = specification["endpoint"]
        if type(endpoint) is not str or endpoint not in {f"{board_name}.{uart}" for uart in _UARTS}:
            raise ManifestError("ready.endpoint must name a supported UART on the declared board")
        ready = Ready(endpoint, text_bytes(specification["text"], "ready.text"))
    return RobotManifest(manifest_path, robot["name"], board, bus, link,
                         tuple(sorted(motors, key=lambda motor: motor.motor_id)),
                         physics, computers, ready)
