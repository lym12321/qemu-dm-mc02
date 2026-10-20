"""Two native QEMUs and upper processes around one admitted joint world.

Duration uses shared logical world time. Native CPU targets and paused CAN
receipts surround the sole world step. UART observations retain their own QEMU
time bounds; target completion does not close equal-time IRQ/device events or
establish wall-clock pacing. Any failure requires recreation of the complete run.
"""

from dataclasses import dataclass
import subprocess
import time

from sim.boards import QemuBoard
from sim.boards.channels import ChannelEvent, LabChannels
from sim.boards.launch import UARTS, positive_seconds, qemu_command, regular_file, resolve_qemu
from sim.core import Session
from sim.devices.dji.protocol import validate_motor_id
from sim.devices.referee import RefereeEngine, RefereeUartEndpoint, validate_referee_events
from sim.devices.referee.engine import MAX_SCRIPT_EVENTS
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.fabric.can import check_can_time
from sim.processes import ChildProcess
from sim.robots.manifest import Ready
from sim.robots.shared_world import (
    BoardBinding, BoardExecution, RefereeBinding, SharedWorldRuntime,
)
from sim.transports import StreamTransport, connect_unix
from sim.transports.can_stream import CanStream
from sim.transports.cosim import StepFramedSocket
from sim.transports import step_codec as codec
from tools.dm_mc02_firmware_rtf import deadline_qmp_call
from tools.dm_mc02_qmp import QmpSession
from tools.dm_mc02_test_harness import running_qemu, stop

from .manifest import (MAX_CAN_DELAY_EVENTS, MAX_MOTOR_FEEDBACK_EVENTS,
                       ArenaManifest, ArenaReferee, ArenaRobot, member_uarts,
                       validate_can_delays)
from .world import create_arena_plant


@dataclass(frozen=True)
class ArenaEvent:
    name: str
    event: ChannelEvent


@dataclass(frozen=True)
class ArenaObservation:
    """Borrowed post-read board snapshot and exact channel batch for one peer.

    The frozen wrapper is shallow. Consumers must treat snapshot/events as
    read-only and must not reenter the runtime. UART event times remain polling
    samples; snapshot["timestamp_ns"] is their actual post-read QEMU bound.
    """

    name: str
    snapshot: dict
    events: list[ChannelEvent]


@dataclass(frozen=True)
class ArenaBoardResult:
    name: str
    prefix: str
    ready: str
    ready_qemu_ns: int | None
    end_qemu_ns: int
    reset_qemu_ns: int
    observation_batches: int


@dataclass(frozen=True)
class ArenaRefereeResult:
    member: str
    uart: str
    period_ns: int
    publications: int
    first_logical_ns: int
    last_logical_ns: int
    next_sequence: int


@dataclass(frozen=True)
class ArenaResult:
    name: str
    origin_ns: int
    end_ns: int
    steps: int
    boards: tuple[ArenaBoardResult, ...]
    children: tuple[dict, ...]
    limits: tuple[str, ...] = (
        "Fixed two-chassis toy profile; physical parameters are uncalibrated.",
        "Duration is shared logical time; QEMU epochs remain independent.",
        "Native CPU cuts do not close equal-time IRQ/device events or establish wall pacing.",
        "Omitted ready markers do not verify application readiness or sensor consumption.",
        "Response callbacks see actual observations, not a complete wire trace.",
        "Child output retains bounded sampled tails and may encounter pipe backpressure.",
        "Active host deadline excludes native admission and bounded resource cleanup; blocked native methods require external timeout.",
    )
    referees: tuple[ArenaRefereeResult, ...] = ()


@dataclass
class _Referee:
    configuration: ArenaReferee
    engine: RefereeEngine
    endpoint: RefereeUartEndpoint | None = None
    publications: int = 0
    first_ns: int | None = None
    last_ns: int | None = None
    next_due_ns: int | None = None


@dataclass
class _Peer:
    member: ArenaRobot
    owner: object
    qmp: object
    board: QemuBoard
    channels: LabChannels
    cosim: StepFramedSocket
    can: CanStream
    last_ns: int = 0
    ready_qemu_ns: int | None = None
    reset_qemu_ns: int = 0
    tail: bytes = b""
    batches: int = 0


class _Run:
    def __init__(self, startup_deadline, deadline, emit, on_observation=None):
        self.startup_deadline, self.deadline = startup_deadline, deadline
        self.emit = emit
        self.on_observation = on_observation
        self.startup = True
        self.peers, self.children = [], []

    def guard(self):
        for child in self.children:
            code = child.process.poll()
            if code is not None:
                error = RuntimeError(f"Arena {child.label} exited early ({code})")
                try:
                    child.pump()
                except BaseException as exc:
                    BaseException.add_note(error, f"Arena {child.label} diagnostic read failed: {exc}")
                raise error
        for peer in self.peers:
            code = peer.owner.process.poll()
            if code is not None:
                raise RuntimeError(f"Arena QEMU {peer.member.name} exited early ({code})")
        remaining = (self.startup_deadline if self.startup else self.deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Arena startup deadline expired" if self.startup else "Arena host deadline expired")
        return remaining

    def call(self, peer, callback):
        self.guard()
        value = deadline_qmp_call(peer.qmp,
            self.startup_deadline if self.startup else self.deadline, "Arena", callback)
        self.guard()
        return value

    def pump(self):
        self.guard()
        for child in self.children:
            child.pump()
            self.guard()
        for peer in self.peers:
            before = self.call(peer, peer.board.snapshot)["timestamp_ns"]
            if before < peer.last_ns:
                raise RuntimeError(f"Arena QEMU {peer.member.name} virtual time moved backwards")
            events = peer.channels.service(before, flush_uart=False)
            self.guard()
            snapshot = self.call(peer, peer.board.snapshot)
            after = snapshot["timestamp_ns"]
            if after < before:
                raise RuntimeError(f"Arena QEMU {peer.member.name} virtual time moved backwards")
            peer.last_ns = after
            peer.batches += 1
            ready = peer.member.robot.ready
            for event in events:
                if self.emit is not None:
                    self.emit(ArenaEvent(peer.member.name, event))
                    self.guard()
                if (ready is not None and peer.ready_qemu_ns is None
                        and event.endpoint == ready.endpoint.split(".", 1)[1]):
                    combined = peer.tail + event.data
                    if ready.text in combined:
                        peer.ready_qemu_ns = after
                    peer.tail = combined[-(len(ready.text) - 1):] if len(ready.text) > 1 else b""
            if self.on_observation is not None:
                self.guard()
                self.on_observation(ArenaObservation(peer.member.name, snapshot, events))
                self.guard()
        return all(peer.member.robot.ready is None or peer.ready_qemu_ns is not None
                   for peer in self.peers)


def run_manifest(manifest: ArenaManifest, *, duration=5.0, emit=None,
                 on_response=None, on_observation=None, qemu=None, startup_timeout=10.0,
                 timeout=30.0, referee_period_ns=100_000_000,
                 referee_events=(), can_delays=(), motor_feedback=()) -> ArenaResult:
    """Consume admitted records and return only after all owners are released.

    Referees publish the latest completed snapshot when their local UART marker
    is observed, then at absolute logical deadlines in the existing tick loop.
    The whole-snapshot cadence is a fixture, not official command periods.
    Capacity refusal terminates the complete Arena without replaying a tick.
    Optional named typed event batches use the shared reset origin and are
    queued once after start, before resume or the first world step. All batches
    are admitted before resources; a runtime refusal terminates the whole run.
    CAN delay actions are (reset-relative offset_ns, member, delay_ns) tuples.
    They split ticks at their exact deadlines and change subsequent admissions
    only, in caller order, after the completed step and before publication.
    Motor feedback actions are (offset_ns, member, motor_id, enabled) tuples,
    using the same reset origin and exact tick splitting. At equal times CAN
    delays precede feedback actions; each schedule retains caller order.
    Feedback actions affect subsequent dispatch, including pending frames;
    feedback already delivered by the completed step cannot be retracted.
    Splitting changes native integration intervals and STEP observations.
    Native public QMP batching bounds each fresh paused CAN observation; liveness
    checks surround the whole group and cannot preempt a blocked callback.
    on_observation receives each peer's actual post-read snapshot and original
    UART event list, including empty batches, after emit and readiness handling.
    It borrows these objects read-only and must not reenter the runtime. A
    callback failure terminates the run without replaying observations.
    """
    if not isinstance(manifest, ArenaManifest):
        raise TypeError("ArenaManifest is required")
    if (type(manifest.referees) is not tuple
            or any(type(item) is not ArenaReferee for item in manifest.referees)):
        raise TypeError("Arena referees require a plain ArenaReferee tuple")
    members = {member.name: member for member in manifest.robots}
    if manifest.referees and len(members) != len(manifest.robots):
        raise ValueError("Arena referee members must be unique")
    configured = {}
    for item in manifest.referees:
        if type(item.member) is not str or item.member not in members:
            raise ValueError("Arena referee must name a declared member")
        if item.member in configured:
            raise ValueError("Arena member must have at most one referee")
        member = members[item.member]
        if type(item.uart) is not str or item.uart != member.robot.board.name + ".uart7":
            raise ValueError("Arena referee UART must be its member board's UART7")
        ready = member.robot.ready
        if (type(ready) is not Ready or type(ready.text) is not bytes or not ready.text
                or type(ready.endpoint) is not str
                or ready.endpoint not in tuple(member.robot.board.name + "." + port
                                              for port in UARTS)):
            raise ValueError("Arena referee requires an explicit member UART ready marker")
        configured[item.member] = item
    if type(referee_period_ns) is not int:
        raise TypeError("referee_period_ns must be an integer")
    if not 1_000_000 <= referee_period_ns <= 60_000_000_000:
        raise ValueError("referee_period_ns must be in 1ms..60s")
    for callback, label in ((emit, "output sink"), (on_response, "response observer"),
                            (on_observation, "observation sink")):
        if callback is not None and not callable(callback):
            raise TypeError(f"Arena {label} must be callable")
    duration = positive_seconds(duration)
    if not 1e-9 <= duration <= 3600:
        raise ValueError("Arena duration must be in 1ns..3600s")
    duration_ns = round(duration * 1_000_000_000)
    if type(referee_events) is not tuple:
        raise TypeError("Arena referee_events must be a plain immutable tuple")
    seen = set()
    count = 0
    for item in referee_events:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("Arena referee_events entries require (member, events) tuples")
        member, events = item
        if type(member) is not str or member not in configured:
            raise ValueError("Arena queued events must name a configured referee member")
        if member in seen:
            raise ValueError("Arena queued referee members must be unique")
        seen.add(member)
        validate_referee_events(events)
        count += len(events)
        if count > MAX_SCRIPT_EVENTS:
            raise ValueError(f"Arena queued referee events total must not exceed {MAX_SCRIPT_EVENTS}")
        if events and events[-1].offset_ns > duration_ns:
            raise ValueError("Arena queued referee event offset must not exceed duration")
    validate_can_delays(can_delays, duration_ns=duration_ns)
    if can_delays and len(members) != len(manifest.robots):
        raise ValueError("Arena CAN delay members must be unique")
    for _, member, _ in can_delays:
        if member not in members:
            raise ValueError("Arena CAN delay action must name a declared member")
    if type(motor_feedback) is not tuple:
        raise TypeError("Arena motor_feedback must be a plain immutable tuple")
    if len(motor_feedback) > MAX_MOTOR_FEEDBACK_EVENTS:
        raise ValueError(f"Arena motor feedback actions must not exceed {MAX_MOTOR_FEEDBACK_EVENTS}")
    if motor_feedback and len(members) != len(manifest.robots):
        raise ValueError("Arena motor feedback members must be unique")
    previous_offset = None
    for item in motor_feedback:
        if type(item) is not tuple or len(item) != 4:
            raise TypeError("Arena motor_feedback entries require (offset_ns, member, motor_id, enabled) tuples")
        offset_ns, member, motor_id, enabled = item
        check_can_time(offset_ns, previous_offset)
        if offset_ns > duration_ns:
            raise ValueError("Arena motor feedback action offset must not exceed duration")
        if type(member) is not str:
            raise ValueError("Arena motor feedback action requires a plain member string")
        validate_motor_id(motor_id)
        if type(enabled) is not bool:
            raise TypeError("Arena motor feedback enabled must be a boolean")
        if member not in members:
            raise ValueError("Arena motor feedback action must name a declared member")
        robot = members[member].robot
        motors = [motor for motor in robot.devices if motor.motor_id == motor_id]
        if (len(motors) != 1 or type(motors[0].motor_id) is not int
                or motors[0].bus != robot.bus.name):
            raise ValueError("Arena motor feedback action must name one declared motor on its member bus")
        previous_offset = offset_ns
    startup_timeout, timeout = positive_seconds(startup_timeout), positive_seconds(timeout)
    if startup_timeout > timeout:
        raise ValueError("startup timeout must not exceed active host timeout")
    binary = resolve_qemu(qemu)
    regular_file(manifest.world.model, "world model")
    commands = []
    for member in manifest.robots:
        firmware = regular_file(member.robot.board.firmware, "firmware")
        uarts = member_uarts(member, referee=member.name in configured)
        sockets = ("qmp", "cosim", "can1", *uarts)
        command = qemu_command(firmware, binary, sockets)
        command += ["-accel", "tcg,thread=single", "-icount", "shift=0,align=off,sleep=off"]
        commands.append((member, uarts, sockets, command))
    runtime = None
    resources, contexts = [], []
    referees = []
    run = None
    primary = None
    plant = None
    try:
        # Register every domain owner immediately, before native admission or
        # processes, so a later factory failure still releases earlier owners.
        for member in manifest.robots:
            if member.name in configured:
                item = configured[member.name]
                engine = RefereeEngine(item.initial, durations=item.durations,
                    events=item.events, cooling_42mm_per_s=item.cooling_42mm_per_s)
                referees.append(_Referee(item, engine))
        plant = create_arena_plant(manifest)
        started = time.monotonic()
        run = _Run(started + startup_timeout, started + timeout, emit, on_observation)
        offset = 0
        bindings = []
        for member, uarts, sockets, command in commands:
            manager = running_qemu(command, sockets=sockets, startup_timeout=run.guard())
            owner = manager.__enter__()
            contexts.append((member.name, manager))
            # Every acquired owner is visible to liveness before the next connect.
            peer = _Peer(member, owner, None, None, None, None, None)
            run.peers.append(peer)
            run.guard()
            qmp = QmpSession(str(owner.sockets["qmp"]), timeout=min(2., run.guard()),
                             connect_timeout=min(2., run.guard()), close_timeout=2.)
            resources.append((member.name + " QMP", qmp))
            peer.qmp = qmp
            if not callable(getattr(qmp, "command_many", None)):
                raise TypeError("Arena QMP requires callable public command_many")
            peer.board = QemuBoard(qmp)
            opened = {}
            for port in ("cosim", "can1", *uarts):
                sock = connect_unix(str(owner.sockets[port]), min(2., run.guard()))
                resources.append((member.name + " " + port, sock))
                opened[port] = sock
                run.guard()
            peer.cosim, peer.can = StepFramedSocket(opened["cosim"]), CanStream(opened["can1"])
            transports = {uart: StreamTransport(opened[uart]) for uart in uarts}
            peer.channels = LabChannels(uarts=transports)
            for referee in referees:
                if referee.configuration.member == member.name:
                    referee.endpoint = RefereeUartEndpoint(transports["uart7"])
            bus = plant.devices[offset].bus
            offset += len(member.robot.devices)
            bindings.append(BoardBinding(member.name, QemuCanEndpoint(bus, peer.can),
                peer.cosim, member.prefix[:-1], BoardExecution(
                    lambda name, arguments=None, peer=peer: run.call(
                        peer, lambda: peer.qmp.command(name, arguments)),
                    "dm-mc02.fdcan1", "can1",
                    lambda requests, peer=peer: run.call(
                        peer, lambda: peer.qmp.command_many(requests)))))
            for upper in member.robot.upper_computers:
                run.guard()
                process = subprocess.Popen(list(upper.command), cwd=upper.cwd, shell=False,
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                child = ChildProcess(f"{member.name}/upper:{upper.name}", process)
                run.children.append(child)
                child.prepare()
                run.guard()
        runtime = SharedWorldRuntime(plant, tuple(bindings),
            referees=tuple(RefereeBinding(item.configuration.member, item.engine, item.endpoint)
                           for item in referees),
            check_running=run.guard, on_response=on_response)
        session = Session(1)
        responses = runtime.start(session, (1, 2), deadline=run.startup_deadline)
        for member, events in referee_events:
            run.guard()
            runtime.queue_referee_events(member, events, session_id=session.session_id)
            run.guard()
        can_delay_index = 0
        motor_feedback_index = 0
        def apply_can_actions():
            nonlocal can_delay_index, motor_feedback_index
            while (can_delay_index < len(can_delays)
                   and session.t_sim_ns + can_delays[can_delay_index][0] <= runtime.t_sim_ns):
                _, member, delay_ns = can_delays[can_delay_index]
                run.guard()
                runtime.set_can_delay(member, delay_ns)
                run.guard()
                can_delay_index += 1
            while (motor_feedback_index < len(motor_feedback)
                   and session.t_sim_ns + motor_feedback[motor_feedback_index][0] <= runtime.t_sim_ns):
                _, member, motor_id, enabled = motor_feedback[motor_feedback_index]
                run.guard()
                runtime.set_motor_feedback_enabled(member, motor_id, enabled)
                run.guard()
                motor_feedback_index += 1
        def advance(dt_ns, *, deadline):
            apply_can_actions()
            if can_delay_index < len(can_delays):
                dt_ns = min(dt_ns, session.t_sim_ns + can_delays[can_delay_index][0]
                            - runtime.t_sim_ns)
            if motor_feedback_index < len(motor_feedback):
                dt_ns = min(dt_ns, session.t_sim_ns + motor_feedback[motor_feedback_index][0]
                            - runtime.t_sim_ns)
            runtime.step(dt_ns, deadline=deadline)
            apply_can_actions()
        apply_can_actions()
        for peer, response in zip(run.peers, responses):
            peer.reset_qemu_ns = codec.decode_reset_ack_payload(response.payload).virtual_time_ns
            peer.last_ns = run.call(peer, peer.board.snapshot)["timestamp_ns"]
        def pump():
            ready = run.pump()
            now = runtime.t_sim_ns
            for item in referees:
                peer = next(peer for peer in run.peers
                            if peer.member.name == item.configuration.member)
                if peer.ready_qemu_ns is None:
                    continue
                if item.next_due_ns is not None and now < item.next_due_ns:
                    continue
                runtime.publish_referee(item.configuration.member,
                    deadline=run.startup_deadline if run.startup else run.deadline)
                item.publications += 1
                item.last_ns = now
                if item.first_ns is None:
                    item.first_ns = now
                    item.next_due_ns = now + referee_period_ns
                else:
                    item.next_due_ns += referee_period_ns
            return ready
        while not pump():
            advance(1_000_000, deadline=run.startup_deadline)
        origin = runtime.t_sim_ns
        run.startup = False
        target = origin + duration_ns
        while runtime.t_sim_ns < target:
            advance(min(1_000_000, target - runtime.t_sim_ns), deadline=run.deadline)
            pump()
        for peer in run.peers:
            run.call(peer, peer.board.pause)
        runtime.service(deadline=run.deadline)
        pump()
        run.guard()
        result = ArenaResult(manifest.name, origin, runtime.t_sim_ns, runtime.step_id,
            tuple(ArenaBoardResult(peer.member.name, peer.member.prefix,
                "uart_marker" if peer.member.robot.ready else "not_waited",
                peer.ready_qemu_ns, peer.last_ns, peer.reset_qemu_ns, peer.batches)
                for peer in run.peers),
            tuple({"label": child.label, "pid": child.process.pid, **child.diagnostics()}
                  for child in run.children),
            referees=tuple(ArenaRefereeResult(item.configuration.member,
                item.configuration.uart, referee_period_ns, item.publications,
                item.first_ns, item.last_ns, item.endpoint.next_sequence)
                for item in referees))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        errors = []
        def release(label, callback):
            try:
                callback()
            except BaseException as exc:
                errors.append((label, exc))
        if primary is None and run is not None:
            release("pre-cleanup liveness", run.guard)
        if runtime is not None:
            # Successful runtime construction transferred every domain owner.
            release("world", runtime.close)
        else:
            for item in reversed(referees):
                if item.endpoint is not None:
                    release("referee " + item.configuration.member + " endpoint", item.endpoint.close)
            for item in reversed(referees):
                release("referee " + item.configuration.member + " engine", item.engine.close)
            if plant is not None:
                release("world", plant.close)
        children = run.children if run is not None else ()
        for child in reversed(children):
            release(child.label + " stop", lambda child=child: stop(child.process))
            for name in ("stdout", "stderr"):
                pipe = getattr(child.process, name)
                if pipe is not None:
                    release(child.label + " " + name, pipe.close)
        for label, resource in reversed(resources):
            release(label, resource.close)
        for label, manager in reversed(contexts):
            error = primary if primary is not None else errors[0][1] if errors else None
            release("QEMU " + label, lambda manager=manager, error=error:
                manager.__exit__(type(error) if error is not None else None,
                                 error, error.__traceback__ if error is not None else None))
        if primary is not None or errors:
            error = primary if primary is not None else errors[0][1]
            for label, exc in errors:
                BaseException.add_note(error, f"Arena cleanup {label} failed: {exc}")
                if exc is not error:
                    for note in tuple(getattr(exc, "__notes__", ())):
                        BaseException.add_note(error, note)
            for child in children:
                BaseException.add_note(error, f"Arena {child.label} output tails: {child.diagnostics()!r}")
            if primary is None:
                raise error
    return result
