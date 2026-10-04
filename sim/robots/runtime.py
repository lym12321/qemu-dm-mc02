"""Native single-board robot process composition over existing public owners.

The worker alone consumes co-sim/CAN; this parent observes native QMP and UART.
QEMU, plant and host clocks remain distinct. Duration uses QEMU virtual time.
"""

from dataclasses import dataclass, field
import os
import select
import subprocess
import sys
import time

from sim.boards import QemuBoard
from sim.boards.channels import LabChannels
from sim.boards.launch import (PROJECT_ROOT, positive_seconds, qemu_command,
                               qmp_socket_path, regular_file, resolve_qemu)
from sim.transports import StreamTransport, connect_unix
from tools.dm_mc02_firmware_rtf import deadline_qmp_call
from tools.dm_mc02_qmp import QmpSession
from tools.dm_mc02_test_harness import running_qemu, stop

from .manifest import RobotManifest


@dataclass
class _Child:
    label: str
    process: subprocess.Popen
    tails: dict = field(default_factory=lambda: {"stdout": b"", "stderr": b""})
    eof: set = field(default_factory=set)

    def prepare(self):
        for name in self.tails:
            os.set_blocking(getattr(self.process, name).fileno(), False)

    def pump(self):
        # One bounded read per pipe, including children with continuous output.
        for name in self.tails:
            if name in self.eof:
                continue
            try:
                data = os.read(getattr(self.process, name).fileno(), 4096)
            except BlockingIOError:
                continue
            if not data:
                self.eof.add(name)
            else:
                self.tails[name] = (self.tails[name] + data)[-12000:]

    def readers(self):
        return [getattr(self.process, name) for name in self.tails if name not in self.eof]

    def diagnostics(self):
        return {name: data.decode("utf-8", errors="replace") for name, data in self.tails.items()}


class _Connection:
    """Borrow connections/processes; apply one startup and active deadline."""

    def __init__(self, owner, qmp, peers, channels, children, startup_deadline, deadline):
        self.owner, self.qmp, self.peers = owner, qmp, peers
        self.channels, self.children = channels, children
        self.board = QemuBoard(qmp)
        self.startup_deadline, self.deadline = startup_deadline, deadline

    def remaining(self, *, startup=False):
        for child in self.children:
            code = child.process.poll()
            if code is not None:
                error = RuntimeError(f"Robot {child.label} exited early ({code})")
                try:
                    child.pump()
                except BaseException as exc:
                    error.add_note(f"Robot {child.label} diagnostic read failed: {exc}")
                raise error
        if self.owner.process.poll() is not None:
            raise RuntimeError(f"QEMU exited during Robot ({self.owner.process.returncode})")
        remaining = (self.startup_deadline if startup else self.deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Robot startup deadline expired" if startup else "Robot host deadline expired")
        return remaining

    def call(self, callback, *, startup=False):
        self.remaining(startup=startup)
        value = deadline_qmp_call(
            self.qmp, self.startup_deadline if startup else self.deadline, "Robot", callback)
        self.remaining(startup=startup)
        return value

    def pump(self, *, startup=False):
        for child in self.children:
            child.pump()
        before = self.call(self.board.snapshot, startup=startup)
        events = self.channels.service(before["timestamp_ns"])
        after = self.call(self.board.snapshot, startup=startup)
        if after["timestamp_ns"] < before["timestamp_ns"]:
            raise RuntimeError("QEMU virtual time moved backwards")
        return after, events

    def wait(self, *, startup=False):
        readers = [*self.peers, *(pipe for child in self.children for pipe in child.readers())]
        select.select(readers, [], [], min(.005, self.remaining(startup=startup)))
        self.remaining(startup=startup)


@dataclass(frozen=True)
class RobotResult:
    name: str
    ready: str
    origin_ns: int
    end_ns: int
    observation_batches: int
    children: tuple[dict, ...]
    limits: tuple[str, ...] = (
        "Single fixed four-wheel profile; physical parameters are uncalibrated.",
        "QEMU and worker/plant epochs are distinct; no global CPU/plant barrier.",
        "Without a UART ready marker, application readiness and plant advancement are not verified.",
        "Child output retains sampled bounded tails; sustained output can encounter OS pipe backpressure.",
        "Active host deadline excludes bounded per-resource cleanup; blocking OS/sinks are not preempted.",
        "Native GDB pauses remain subject to worker ACK and host deadlines.",
    )


def run_manifest(manifest: RobotManifest, *, duration=5.0, emit=None,
                 qemu=None, startup_timeout=10.0, timeout=30.0,
                 qmp_path=None, gdb_port=None) -> RobotResult:
    """Run an admitted robot and return only after independent owner cleanup."""
    if not isinstance(manifest, RobotManifest):
        raise TypeError("RobotManifest is required")
    if emit is not None and not callable(emit):
        raise TypeError("Robot output sink must be callable")
    duration = positive_seconds(duration)
    if duration > 3600 or duration < 1e-9:
        raise ValueError("Robot duration must be in 1ns..3600s")
    duration_ns = round(duration * 1_000_000_000)
    startup_timeout, timeout = positive_seconds(startup_timeout), positive_seconds(timeout)
    if startup_timeout > timeout:
        raise ValueError("startup timeout must not exceed active host timeout")
    if gdb_port is not None and (type(gdb_port) is not int or not 1 <= gdb_port <= 65535):
        raise ValueError("GDB port must be an integer in 1..65535")
    binary = resolve_qemu(qemu)
    firmware = regular_file(manifest.board.firmware, "firmware")
    regular_file(manifest.physics.model, "physics model")
    external_qmp = qmp_socket_path(qmp_path)
    uart = manifest.ready.endpoint.split(".", 1)[1] if manifest.ready else "uart1"
    sockets = ("qmp", "cosim", "can1", uart)
    command = qemu_command(firmware, binary, sockets, qmp_path=external_qmp, gdb_port=gdb_port)
    children, resources = [], []
    primary = None
    with running_qemu(command, sockets=sockets, startup_timeout=startup_timeout) as owner:
        startup_deadline = owner.start_ns / 1e9 + startup_timeout
        deadline = owner.start_ns / 1e9 + timeout
        try:
            def connection_timeout():
                value = startup_deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError("Robot connection startup deadline expired")
                return min(2.0, value)

            qmp = QmpSession(str(owner.sockets["qmp"]), timeout=connection_timeout(),
                             connect_timeout=connection_timeout(), close_timeout=2.0)
            resources.append(("QMP", qmp))
            sock = connect_unix(str(owner.sockets[uart]), connection_timeout())
            resources.append((uart, sock))
            channels = LabChannels(uarts={uart: StreamTransport(sock)})
            worker_command = [sys.executable, str(PROJECT_ROOT / "tools/run-chassis-worker.py"),
                              "--cosim", str(owner.sockets["cosim"]), "--fdcan", str(owner.sockets["can1"]),
                              "--chassis-model", str(manifest.physics.model), "--protocol", "v2",
                              "--rate", "1000", "--realtime", "--frames", "0",
                              "--connect-timeout", str(connection_timeout())]
            for label, argv, cwd in (("worker", worker_command, manifest.path.parent),
                                     *((f"upper:{upper.name}", list(upper.command), upper.cwd)
                                       for upper in manifest.upper_computers)):
                connection_timeout()
                process = subprocess.Popen(argv, cwd=cwd, shell=False, start_new_session=True,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                child = _Child(label, process)
                children.append(child)  # Admission/configuration failures are owned too.
                child.prepare()
                connection_timeout()
            connection = _Connection(owner, qmp, [sock], channels, children, startup_deadline, deadline)
            origin = connection.call(connection.board.snapshot, startup=True)["timestamp_ns"]
            connection.call(connection.board.resume, startup=True)
            batches, tail = 0, b""
            ready_status = "not_waited"
            if manifest.ready is not None:
                while True:
                    state, events = connection.pump(startup=True)
                    batches += 1
                    seen = False
                    for event in events:
                        if emit is not None:
                            emit(event)
                        combined = tail + event.data
                        seen |= manifest.ready.text in combined
                        tail = combined[-(len(manifest.ready.text) - 1):] if len(manifest.ready.text) > 1 else b""
                    if seen:
                        origin = state["timestamp_ns"]
                        ready_status = "uart_marker"
                        break
                    connection.wait(startup=True)
            previous = origin
            while True:
                state, events = connection.pump()
                batches += 1
                stamp = state["timestamp_ns"]
                if stamp < previous:
                    raise RuntimeError("QEMU virtual time moved backwards")
                previous = stamp
                for event in events:
                    if emit is not None:
                        emit(event)
                if stamp - origin >= duration_ns:
                    connection.call(connection.board.pause)
                    connection.remaining()
                    break
                connection.wait()
            result = RobotResult(manifest.name, ready_status, origin, stamp, batches, tuple(
                {"label": child.label, "pid": child.process.pid, **child.diagnostics()}
                for child in children))
        except BaseException as exc:
            primary = exc
            raise
        finally:
            errors = []
            for child in reversed(children):
                try:
                    stop(child.process)
                except BaseException as exc:
                    errors.append((child.label + " stop", exc))
                for name in ("stdout", "stderr"):
                    pipe = getattr(child.process, name)
                    if pipe is not None:
                        try:
                            pipe.close()
                        except BaseException as exc:
                            errors.append((child.label + " " + name, exc))
            for label, resource in reversed(resources):
                try:
                    resource.close()
                except BaseException as exc:
                    errors.append((label, exc))
            if primary is not None or errors:
                error = primary if primary is not None else errors[0][1]
                for label, exc in errors:
                    BaseException.add_note(error, f"Robot cleanup {label} failed: {exc}")
                for child in children:
                    BaseException.add_note(error, f"Robot {child.label} output tails: {child.diagnostics()!r}")
                if primary is None:
                    raise error
    return result
