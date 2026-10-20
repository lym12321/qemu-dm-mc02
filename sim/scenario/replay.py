"""Reevaluate recorded Arena observations with the existing Lab assertions.

Completion origins are trailing records. Two complete passes over a borrowed
seekable input avoid retaining observations: discover metadata, then evaluate
private LabExperiment state and independently validate the second transcript.
No runtime, input dispatch, hardware callbacks or replay clock is owned here.
"""

from dataclasses import dataclass

from sim.arena.manifest import ArenaManifest, ArenaReferee, ArenaRobot, member_uarts
from sim.arena.trace import (
    COMPLETION_TYPE, EVENT_TYPE, ArenaTraceCompletion,
    decode_completion, decode_observation, validate_clocks,
)
from sim.boards.launch import UARTS
from sim.lab.experiment import LabExperiment
from sim.lab.manifest import LabManifest
from sim.config.yaml import MAX_TIME_NS, identifier
from sim.robots.manifest import Board, Ready, RobotManifest
from sim.trace import iter_events

from .manifest import ScenarioManifest, validate_assertions


@dataclass(frozen=True)
class ScenarioReplayResult:
    """Terminal statuses in original assertion order; members in manifest order."""

    status: tuple[str, ...]
    failures: tuple[str, ...]
    completions: tuple[ArenaTraceCompletion, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(value == "pass" for value in self.status)


def _configuration(manifest, clocks):
    if type(manifest) is not ScenarioManifest or type(manifest.arena) is not ArenaManifest:
        raise TypeError("Scenario replay requires a ScenarioManifest and ArenaManifest")
    if type(manifest.duration_ns) is not int:
        raise TypeError("Scenario replay duration_ns must be a plain integer")
    if not 1 <= manifest.duration_ns <= MAX_TIME_NS:
        raise ValueError("Scenario replay duration_ns must be in 1ns..3600s")
    members = manifest.arena.robots
    if (type(members) is not tuple or not 1 <= len(members) <= 128
            or any(type(member) is not ArenaRobot for member in members)):
        raise TypeError("Scenario replay requires a tuple of 1..128 ArenaRobot members")
    clocks = validate_clocks(clocks)
    if any(type(member.name) is not str for member in members):
        raise TypeError("Scenario replay member names must be plain strings")
    names = [member.name for member in members]
    for name in names:
        identifier(name, "Scenario replay member")
    if len(set(names)) != len(names) or set(names) != clocks.keys():
        raise ValueError("Scenario replay clocks must cover exactly the unique Arena members")
    referees = manifest.arena.referees
    if (type(referees) is not tuple or len(referees) > len(members)
            or any(type(item) is not ArenaReferee or type(item.member) is not str
                   for item in referees)):
        raise TypeError("Scenario replay requires an ArenaReferee tuple")
    configured = {item.member: item for item in referees}
    if len(configured) != len(referees) or not configured.keys() <= clocks.keys():
        raise ValueError("Scenario replay referee members must be unique Arena members")
    uarts = {}
    for member in members:
        robot = member.robot
        if type(robot) is not RobotManifest or type(robot.board) is not Board:
            raise TypeError("Scenario replay requires typed RobotManifest and Board records")
        if type(robot.board.name) is not str:
            raise TypeError("Scenario replay board names must be plain strings")
        identifier(robot.board.name, "Scenario replay board")
        if type(robot.board.board) is not str or robot.board.board != "dm-mc02":
            raise ValueError("Scenario replay requires the dm-mc02 observation profile")
        ready = robot.ready
        if ready is not None:
            if (type(ready) is not Ready or type(ready.endpoint) is not str
                    or type(ready.text) is not bytes):
                raise TypeError("Scenario replay requires a typed UART Ready record")
            if (not ready.text or ready.endpoint not in
                    tuple(robot.board.name + "." + port for port in UARTS)):
                raise ValueError("Scenario replay Ready must name its board UART and a nonempty marker")
        if member.name in configured:
            referee = configured[member.name]
            if (type(referee.uart) is not str or referee.uart != robot.board.name + ".uart7"
                    or ready is None):
                raise ValueError("Scenario replay referee requires its board UART7 and explicit Ready")
        ports = member_uarts(member, referee=member.name in configured)
        if any(port not in UARTS for port in ports):
            raise ValueError("Scenario replay member has an unsupported UART endpoint")
        uarts[member.name] = ports
    validate_assertions(manifest.assertions, arena=manifest.arena,
                        duration_ns=manifest.duration_ns)
    return clocks, uarts


def _pass(source, clocks, uarts, *, expected=None, experiments=None):
    """One complete lifecycle scan, optionally feeding private evaluators."""
    observed, completed = {}, {}
    for event in iter_events(source):
        if event.event_type == EVENT_TYPE:
            if completed:
                raise ValueError("Scenario replay observation follows completion metadata")
            item = decode_observation(event, clocks=clocks)
            if any(entry.endpoint not in uarts[item.name] for entry in item.events):
                raise ValueError("Scenario replay UART was not configured for this member")
            stamp = item.snapshot["timestamp_ns"]
            lower = min((entry.timestamp_ns for entry in item.events), default=stamp)
            count, first, previous = observed.get(item.name, (0, lower, 0))
            if count and (stamp < previous or lower < previous):
                raise ValueError("Scenario replay member observation time moved backwards")
            if count == 0xFFFFFFFF:
                raise OverflowError("Scenario replay observation count exceeds uint32")
            observed[item.name] = count + 1, first, stamp
            if experiments is not None:
                experiments[item.name].observe(item.snapshot, item.events, stamp)
        elif event.event_type == COMPLETION_TYPE:
            item = decode_completion(event, clocks=clocks)
            if item.name in completed or item.name not in observed:
                raise ValueError("Scenario replay requires one completion after member observations")
            count, first, last = observed[item.name]
            if (count != item.observation_batches or first < item.reset_qemu_ns
                    or last != item.end_qemu_ns):
                raise ValueError("Scenario replay completion differs from captured history")
            if expected is not None and item != expected[item.name]:
                raise ValueError("Scenario replay completion changed between passes")
            completed[item.name] = item
        else:
            raise ValueError("Scenario replay contains an unsupported event type")
    if completed.keys() != clocks.keys():
        raise ValueError("Scenario replay lacks complete member coverage")
    return completed


def replay_assertions(source, manifest: ScenarioManifest, *,
                      clocks: dict[str, str]) -> ScenarioReplayResult:
    """Validate twice and return terminal assertions for the second transcript.

    Supply the original admitted configuration and true member/epoch labels.
    Input must provide blocking binary read/seek/tell, start at zero and remain
    immutable during this call. Success leaves EOF after one rewind; failures
    propagate without retry, close or position restoration. A second transcript
    with identical completion metadata is evaluated independently; byte identity
    and provenance are not authenticated. No result escapes before full EOF.

    All member histories and configured UARTs are checked, including unasserted
    members. False assertions return fail statuses; malformed histories/containers
    raise. Scenario input actions, firmware and physics are not executed.
    """
    clocks, uarts = _configuration(manifest, clocks)
    if any(not callable(getattr(source, name, None)) for name in ("read", "seek", "tell")):
        raise TypeError("Scenario replay input requires read/seek/tell")
    position = source.tell()
    if type(position) is not int or position != 0:
        raise ValueError("Scenario replay input must start at byte zero")
    completed = _pass(source, clocks, uarts)
    grouped = {member.name: [] for member in manifest.arena.robots}
    for index, item in enumerate(manifest.assertions):
        grouped[item.member].append((index, item.assertion))
    experiments = {
        member.name: LabExperiment(LabManifest(
            manifest.path, member.name, "dm-mc02", member.robot.board.firmware,
            manifest.duration_ns, (), tuple(item for _, item in grouped[member.name])),
            completed[member.name].reset_qemu_ns)
        for member in manifest.arena.robots
    }
    source.seek(0)
    _pass(source, clocks, uarts, expected=completed, experiments=experiments)
    statuses = [None] * len(manifest.assertions)
    failures = {}
    for name, experiment in experiments.items():
        experiment.finish(completed[name].end_qemu_ns)
        messages = iter(experiment.failures)
        for (index, _), status in zip(grouped[name], experiment.status, strict=True):
            statuses[index] = status
            if status == "fail":
                failures[index] = f"assertion[{index + 1}] member {name!r}: {next(messages)}"
    return ScenarioReplayResult(tuple(statuses), tuple(failures[index] for index in sorted(failures)),
                               tuple(completed[member.name] for member in manifest.arena.robots))
