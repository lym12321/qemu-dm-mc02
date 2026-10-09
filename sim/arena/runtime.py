"""Two native QEMUs and upper processes around one admitted joint world.

Duration uses shared logical world time. UART observations retain their own
QEMU time bounds; ACKs and bounded IO waits do not establish a CPU barrier or
wall-clock pacing. Any failure requires recreation of the complete run.
"""

from dataclasses import dataclass
import select
import subprocess
import time

from sim.boards import QemuBoard
from sim.boards.channels import ChannelEvent, LabChannels
from sim.boards.launch import positive_seconds, qemu_command, regular_file, resolve_qemu
from sim.core import Session
from sim.fabric.endpoints import QemuCanEndpoint
from sim.processes import ChildProcess
from sim.robots.shared_world import BoardBinding, SharedWorldRuntime
from sim.transports import StreamTransport, connect_unix
from sim.transports.can_stream import CanStream
from sim.transports.cosim import StepFramedSocket
from sim.transports import step_codec as codec
from tools.dm_mc02_firmware_rtf import deadline_qmp_call
from tools.dm_mc02_qmp import QmpSession
from tools.dm_mc02_test_harness import running_qemu, stop

from .manifest import ArenaManifest, ArenaRobot
from .world import create_arena_plant


@dataclass(frozen=True)
class ArenaEvent:
    name: str
    event: ChannelEvent


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
        "ACK/DONE and bounded IO waits do not establish CPU lockstep or wall pacing.",
        "Omitted ready markers do not verify application readiness or sensor consumption.",
        "Response callbacks see actual observations, not a complete wire trace.",
        "Child output retains bounded sampled tails and may encounter pipe backpressure.",
        "Active host deadline excludes native admission and bounded resource cleanup; blocked native methods require external timeout.",
    )


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
    def __init__(self, startup_deadline, deadline, emit):
        self.startup_deadline, self.deadline = startup_deadline, deadline
        self.emit = emit
        self.startup = True
        self.peers, self.children, self.readers = [], [], []

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
            events = peer.channels.service(before)
            self.guard()
            after = self.call(peer, peer.board.snapshot)["timestamp_ns"]
            if after < before:
                raise RuntimeError(f"Arena QEMU {peer.member.name} virtual time moved backwards")
            peer.last_ns = after
            peer.batches += 1
            ready = peer.member.robot.ready
            for event in events:
                if self.emit is not None:
                    self.emit(ArenaEvent(peer.member.name, event))
                    self.guard()
                if ready is not None and peer.ready_qemu_ns is None:
                    combined = peer.tail + event.data
                    if ready.text in combined:
                        peer.ready_qemu_ns = after
                    peer.tail = combined[-(len(ready.text) - 1):] if len(ready.text) > 1 else b""
        return all(peer.member.robot.ready is None or peer.ready_qemu_ns is not None
                   for peer in self.peers)

    def wait(self):
        readers = [*self.readers, *(pipe for child in self.children for pipe in child.readers())]
        select.select(readers, [], [], min(.001, self.guard()))
        self.guard()


def run_manifest(manifest: ArenaManifest, *, duration=5.0, emit=None,
                 on_response=None, qemu=None, startup_timeout=10.0,
                 timeout=30.0) -> ArenaResult:
    """Consume admitted records and return only after all owners are released."""
    if not isinstance(manifest, ArenaManifest):
        raise TypeError("ArenaManifest is required")
    if manifest.referees:
        raise ValueError("Arena referee execution is not yet supported")
    for callback, label in ((emit, "output sink"), (on_response, "response observer")):
        if callback is not None and not callable(callback):
            raise TypeError(f"Arena {label} must be callable")
    duration = positive_seconds(duration)
    if not 1e-9 <= duration <= 3600:
        raise ValueError("Arena duration must be in 1ns..3600s")
    duration_ns = round(duration * 1_000_000_000)
    startup_timeout, timeout = positive_seconds(startup_timeout), positive_seconds(timeout)
    if startup_timeout > timeout:
        raise ValueError("startup timeout must not exceed active host timeout")
    binary = resolve_qemu(qemu)
    regular_file(manifest.world.model, "world model")
    commands = []
    for member in manifest.robots:
        firmware = regular_file(member.robot.board.firmware, "firmware")
        uart = member.robot.ready.endpoint.split(".", 1)[1] if member.robot.ready else "uart1"
        sockets = ("qmp", "cosim", "can1", uart)
        commands.append((member, uart, sockets, qemu_command(firmware, binary, sockets)))
    runtime = None
    resources, contexts = [], []
    run = None
    primary = None
    plant = create_arena_plant(manifest)
    try:
        started = time.monotonic()
        run = _Run(started + startup_timeout, started + timeout, emit)
        offset = 0
        bindings = []
        for member, uart, sockets, command in commands:
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
            peer.qmp, peer.board = qmp, QemuBoard(qmp)
            opened = {}
            for port in ("cosim", "can1", uart):
                sock = connect_unix(str(owner.sockets[port]), min(2., run.guard()))
                resources.append((member.name + " " + port, sock))
                run.readers.append(sock)
                opened[port] = sock
                run.guard()
            peer.cosim, peer.can = StepFramedSocket(opened["cosim"]), CanStream(opened["can1"])
            peer.channels = LabChannels(uarts={uart: StreamTransport(opened[uart])})
            bus = plant.devices[offset].bus
            offset += len(member.robot.devices)
            bindings.append(BoardBinding(member.name, QemuCanEndpoint(bus, peer.can),
                                          peer.cosim, member.prefix[:-1]))
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
            check_running=run.guard, on_response=on_response)
        responses = runtime.start(Session(1), (1, 2), deadline=run.startup_deadline)
        for peer, response in zip(run.peers, responses):
            peer.reset_qemu_ns = codec.decode_reset_ack_payload(response.payload).virtual_time_ns
            peer.last_ns = run.call(peer, peer.board.snapshot)["timestamp_ns"]
        for peer in run.peers:
            run.call(peer, peer.board.resume)
        while not run.pump():
            runtime.step(1_000_000, deadline=run.startup_deadline)
            run.wait()
        origin = runtime.t_sim_ns
        run.startup = False
        target = origin + duration_ns
        while runtime.t_sim_ns < target:
            runtime.step(min(1_000_000, target - runtime.t_sim_ns), deadline=run.deadline)
            run.pump()
            if runtime.t_sim_ns < target:
                run.wait()
        for peer in run.peers:
            run.call(peer, peer.board.pause)
        runtime.service(deadline=run.deadline)
        run.pump()
        run.guard()
        result = ArenaResult(manifest.name, origin, runtime.t_sim_ns, runtime.step_id,
            tuple(ArenaBoardResult(peer.member.name, peer.member.prefix,
                "uart_marker" if peer.member.robot.ready else "not_waited",
                peer.ready_qemu_ns, peer.last_ns, peer.reset_qemu_ns, peer.batches)
                for peer in run.peers),
            tuple({"label": child.label, "pid": child.process.pid, **child.diagnostics()}
                  for child in run.children))
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
        release("world", runtime.close if runtime is not None else plant.close)
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
