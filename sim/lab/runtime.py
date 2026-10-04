"""Bounded public QMP/chardev consumer for single-board Lab experiments.

QEMU owns virtual time. Polling dispatch may be late; a native timestamp is
an observation, not a CPU or peripheral barrier. No guest memory is accessed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import select
import shutil
import time

from sim.boards import QemuBoard
from sim.boards.channels import ChannelEvent, LabChannels
from sim.transports import StreamTransport, connect_unix
from tools.dm_mc02_firmware_rtf import deadline_qmp_call
from tools.dm_mc02_qmp import QmpSession
from tools.dm_mc02_sim_worker import CanStream, FramedSocket, TELEMETRY
from tools.dm_mc02_test_harness import running_qemu

from .experiment import LabExperiment
from .manifest import LabManifest, Stimulus


UARTS = ("uart1", "uart2", "uart3", "uart5", "uart7", "uart10")
CANS = ("can1", "can2", "can3")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def positive_seconds(value) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value <= 0):
        raise ValueError("timeout must be finite and positive")
    return float(value)


def _regular_file(path, label: str, *, executable=False) -> Path:
    path = Path(path).resolve()
    if not path.is_file() or not os.access(path, os.X_OK if executable else os.R_OK):
        raise ValueError(f"{label} is not {'executable' if executable else 'readable'}: {path}")
    return path


def _qemu_path(value) -> Path:
    value = value or os.environ.get("QEMU_SYSTEM_ARM") or PROJECT_ROOT / "build/qemu/qemu-system-arm"
    if isinstance(value, str) and "/" not in value:
        value = shutil.which(value) or value
    return _regular_file(value, "QEMU", executable=True)


def _external_qmp(path) -> Path | None:
    if path is None:
        return None
    if not isinstance(path, (str, Path)) or "\0" in str(path):
        raise ValueError("QMP socket must be a path without NUL")
    path = Path(path).absolute()
    if len(os.fsencode(path)) > 100 or not path.parent.is_dir() or path.exists():
        raise ValueError("QMP socket needs an existing parent, a new path, and at most 100 bytes")
    return path


class ConnectedLab:
    """Own protocol peers; borrow the harness's process and private paths."""

    def __init__(self, owner, qmp, peers, channels, startup_deadline, deadline):
        self.owner = owner
        self.qmp = qmp
        self.peers = peers
        self.channels = channels
        self.board = QemuBoard(qmp)
        self.startup_deadline = startup_deadline
        self.deadline = deadline
        self._last_adc_ns = None

    def remaining(self, *, startup=False) -> float:
        if self.owner.process.poll() is not None:
            raise RuntimeError(f"QEMU exited during Lab ({self.owner.process.returncode})")
        remaining = (self.startup_deadline if startup else self.deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Lab startup deadline expired" if startup else "Lab host deadline expired")
        return remaining

    def call(self, callback, *, startup=False):
        self.remaining(startup=startup)
        return deadline_qmp_call(
            self.qmp, self.startup_deadline if startup else self.deadline,
            "Lab", callback)

    def snapshot(self, *, startup=False) -> dict:
        return self.call(self.board.snapshot, startup=startup)

    def pump(self, *, startup=False) -> tuple[dict, list[ChannelEvent]]:
        before = self.snapshot(startup=startup)
        events = self.channels.service(before["timestamp_ns"])
        after = self.snapshot(startup=startup)
        if after["timestamp_ns"] < before["timestamp_ns"]:
            raise RuntimeError("QEMU virtual time moved backwards")
        return after, events

    def wait(self, *, startup=False, maximum=.005) -> None:
        # This wait yields host IO; all experiment scheduling uses QEMU time.
        select.select(self.peers, [], [], min(maximum, self.remaining(startup=startup)))

    def inject(self, stimulus: Stimulus) -> int:
        """Admit at real QEMU time; return its observation sample, not a barrier."""
        stamp = self.snapshot()["timestamp_ns"]
        if stimulus.kind == "uart":
            self.channels.send_uart(stimulus.endpoint, stimulus.value)
        elif stimulus.kind == "can":
            self.channels.send_can(stimulus.endpoint, replace(stimulus.value, timestamp_ns=stamp))
        elif stimulus.kind == "key":
            self.call(lambda: self.board.set_key(stimulus.value))
        elif stimulus.kind == "gpio":
            self.call(lambda: self.board.set_gpio(stimulus.endpoint[5:], stimulus.value))
        elif stimulus.kind in ("adc_raw", "adc_voltage"):
            while stamp <= self._last_adc_ns:
                self.wait()
                stamp = self.snapshot()["timestamp_ns"]
            kwargs = {"raw" if stimulus.kind == "adc_raw" else "voltage_uv": stimulus.value}
            self.channels.send_adc(int(stimulus.endpoint[7:]), timestamp_ns=stamp, **kwargs)
            self._last_adc_ns = stamp
        else:
            raise ValueError(f"unsupported Lab stimulus: {stimulus.kind}")
        return stamp


@contextmanager
def open_lab(firmware, *, qemu=None, uarts=(), cans=(), adc=False,
             startup_timeout=10.0, timeout=30.0, qmp_path=None, gdb_port=None):
    """Start paused, admit every option before process effects, clean all peers."""
    firmware = _regular_file(firmware, "firmware")
    qemu = _qemu_path(qemu)
    startup_timeout = positive_seconds(startup_timeout)
    timeout = positive_seconds(timeout)
    if startup_timeout > timeout:
        raise ValueError("startup timeout must not exceed total host timeout")
    uarts, cans = tuple(uarts), tuple(cans)
    if len(set(uarts)) != len(uarts) or any(port not in UARTS for port in uarts):
        raise ValueError("UART endpoints must be distinct uart1/2/3/5/7/10")
    if len(set(cans)) != len(cans) or any(port not in CANS for port in cans):
        raise ValueError("CAN endpoints must be distinct can1/2/3")
    if type(adc) is not bool:
        raise ValueError("ADC selection must be boolean")
    if gdb_port is not None and (type(gdb_port) is not int or not 1 <= gdb_port <= 65535):
        raise ValueError("GDB port must be an integer in 1..65535")
    external_qmp = _external_qmp(qmp_path)
    command = [str(qemu), "-machine", "dm-mc02,fdcan-host-ack=on", "-kernel", str(firmware),
               "-nodefaults", "-display", "none", "-monitor", "none", "-S",
               "-qmp", "unix:{qmp},server=on,wait=off"]
    sockets = ["qmp", *uarts, *cans]
    if adc:
        sockets.append("cosim")
    for port in ("cosim", *UARTS, *CANS):
        if port in sockets:
            command += ["-chardev", f"socket,id={port},path={{{port}}},server=on,wait=off",
                        "-serial", f"chardev:{port}"]
        else:
            command += ["-serial", "none"]
    if external_qmp is not None:
        command += ["-qmp", f"unix:{str(external_qmp).replace(',', ',,')},server=on,wait=off"]
    if gdb_port is not None:
        command += ["-gdb", f"tcp:127.0.0.1:{gdb_port}"]
    with running_qemu(command, sockets=sockets, startup_timeout=startup_timeout) as owner:
        startup_deadline = owner.start_ns / 1e9 + startup_timeout
        deadline = owner.start_ns / 1e9 + timeout
        resources = []
        primary = None
        try:
            def remaining():
                value = startup_deadline - time.monotonic()
                if value <= 0:
                    raise TimeoutError("Lab connection startup deadline expired")
                return min(2.0, value)

            qmp = QmpSession(str(owner.sockets["qmp"]), timeout=remaining(),
                             connect_timeout=remaining(), close_timeout=2.0)
            resources.append(("QMP", qmp))
            streams = {}
            peers = []
            for port in sockets[1:]:
                sock = connect_unix(str(owner.sockets[port]), remaining())
                peers.append(sock)
                resources.append((port, sock))
                wrapper = FramedSocket if port == "cosim" else CanStream if port in CANS else StreamTransport
                streams[port] = wrapper(sock)
            channels = LabChannels(uarts={port: streams[port] for port in uarts},
                                   cans={port: streams[port] for port in cans},
                                   cosim=streams.get("cosim"))
            lab = ConnectedLab(owner, qmp, peers, channels, startup_deadline, deadline)
            if adc:
                opened = False
                while not opened:
                    lab.remaining(startup=True)
                    for kind, _sequence, _stamp, payload in streams["cosim"].receive():
                        if kind != TELEMETRY or len(payload) != 12:
                            raise RuntimeError("unexpected Lab co-sim opening frame")
                        opened = True
                    if not opened:
                        lab.wait(startup=True)
            stamp = lab.snapshot(startup=True)["timestamp_ns"]
            channels.reset(stamp)
            lab._last_adc_ns = stamp
            yield lab
        except BaseException as exc:
            primary = exc
            raise
        finally:
            errors = []
            for label, resource in reversed(resources):
                try:
                    resource.close()
                except BaseException as exc:
                    errors.append((label, exc))
            if errors:
                error = primary if primary is not None else errors[0][1]
                for label, exc in errors:
                    BaseException.add_note(error, f"Lab cleanup {label} failed: {exc}")
                if primary is None:
                    raise error


@dataclass(frozen=True)
class LabResult:
    name: str
    origin_ns: int
    end_ns: int
    status: tuple[str, ...]
    failures: tuple[str, ...]
    dispatched: tuple[tuple[int, int, str], ...]
    observation_batches: int

    @property
    def passed(self) -> bool:
        return not self.failures and all(status == "pass" for status in self.status)


def run_manifest(manifest: LabManifest, *, emit=None, paused=False, **options) -> LabResult:
    """Run one manifest; return only after protocol and process cleanup succeeds."""
    if not isinstance(manifest, LabManifest) or type(paused) is not bool:
        raise TypeError("LabManifest and a boolean paused option are required")
    if emit is not None and not callable(emit):
        raise TypeError("Lab output sink must be callable")
    if paused and options.get("gdb_port") is None:
        raise ValueError("paused Lab run requires a native GDB port")
    endpoints = {item.endpoint for item in (*manifest.stimulus, *manifest.assertions)}
    if manifest.ready is not None:
        endpoints.add(manifest.ready.endpoint)
    batches = 0
    with open_lab(manifest.firmware, uarts=tuple(port for port in UARTS if port in endpoints),
                  cans=tuple(port for port in CANS if port in endpoints),
                  adc=any(port.startswith("adc1.") for port in endpoints), **options) as lab:
        origin = lab.snapshot(startup=True)["timestamp_ns"]
        if paused:
            while not lab.call(lambda: lab.qmp.command("query-status"), startup=True)["running"]:
                lab.wait(startup=True)
        else:
            lab.call(lab.board.resume, startup=True)
        if manifest.ready is not None:
            tail = b""
            while True:
                state, events = lab.pump(startup=True)
                batches += 1
                ready = False
                for event in events:
                    if emit is not None:
                        emit(event)
                    if event.endpoint == manifest.ready.endpoint:
                        combined = tail + event.data
                        ready |= manifest.ready.text in combined
                        tail = combined[-(len(manifest.ready.text) - 1):] if len(manifest.ready.text) > 1 else b""
                if ready:
                    origin = state["timestamp_ns"]
                    break
                lab.wait(startup=True)
        experiment = LabExperiment(manifest, origin)
        dispatched = []
        while True:
            state, events = lab.pump()
            batches += 1
            if emit is not None:
                for event in events:
                    emit(event)
            stamp = state["timestamp_ns"]
            experiment.observe(state, events, stamp)
            for stimulus in experiment.take_due(stamp):
                actual = lab.inject(stimulus)
                dispatched.append((stimulus.at_ns, actual, stimulus.endpoint))
            if stamp - origin >= manifest.duration_ns:
                # Flush final at=duration injections without pretending an
                # additional CPU observation window exists.
                state, events = lab.pump()
                batches += 1
                if emit is not None:
                    for event in events:
                        emit(event)
                experiment.observe(state, events, state["timestamp_ns"])
                experiment.finish(state["timestamp_ns"])
                break
            lab.wait()
        lab.call(lab.board.pause)
        result = LabResult(manifest.name, origin, state["timestamp_ns"], experiment.status,
                           experiment.failures, tuple(dispatched), batches)
    return result
