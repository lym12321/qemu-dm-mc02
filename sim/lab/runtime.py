"""Bounded public QMP/chardev consumer for single-board Lab experiments.

QEMU owns virtual time. Polling dispatch may be late; a native timestamp is
an observation, not a CPU or peripheral barrier. No guest memory is accessed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import select
import time

from sim.boards import QemuBoard
from sim.boards.channels import ChannelEvent, LabChannels
from sim.boards.launch import (UARTS, CANS, PROJECT_ROOT, positive_seconds,
                               regular_file as _regular_file, resolve_qemu as _qemu_path,
                               qmp_socket_path as _external_qmp, qemu_command)
from sim.transports import StreamTransport, connect_unix
from sim.machines.qemu.deadline import deadline_qmp_call
from sim.machines.qemu.qmp import qmp_session_class
from sim.transports.can_stream import CanStream
from sim.transports.cosim import FramedSocket, TELEMETRY
from sim.machines.qemu.process import running_qemu

from .manifest import Stimulus


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
    sockets = ["qmp", *uarts, *cans]
    if adc:
        sockets.append("cosim")
    command = qemu_command(firmware, qemu, sockets,
                           qmp_path=external_qmp, gdb_port=gdb_port)
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

            qmp = qmp_session_class()(str(owner.sockets["qmp"]), timeout=remaining(),
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
