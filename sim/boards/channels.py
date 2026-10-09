"""Borrow bounded UART, FDCAN and ADC channels for an interactive Lab.

The caller supplies QEMU virtual time; servicing these channels never advances
a CPU or plant. CAN uses the existing Fabric with no loopback. Its observations
retain original QEMU wire timestamps, while injected frames use the Fabric's
delivery time. UART timestamps are caller-supplied polling samples, not byte
times or proven upper bounds. A consumer needing a deadline bound must query
QEMU virtual time after the read and retain that observation bound separately.

The ``tools.dm_mc02_sim_worker`` import is a temporary migration boundary to
the canonical v1 ADC codec, timestamp mapper and stream wrappers. This adapter
does not instantiate a worker, physics backend or its runtime state machine.
All transports and sockets remain owned by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sim.fabric import CanBus, CanFrame
from sim.fabric.can import CanDelivery, check_can_time
from sim.fabric.endpoints import QemuCanEndpoint
from sim.transports import FixedRecordStream, LengthPrefixedStream, StreamTransport
from tools.dm_mc02_sim_worker import (
    ADC_VOLTAGE_MAX_UV, CAN_WIRE_SIZE, HEADER, MAX_PAYLOAD, RESET,
    QemuTimestampMapper, adc_input_frame, adc_voltage_frame, protocol_frame,
)


@dataclass(frozen=True)
class ChannelEvent:
    endpoint: str
    timestamp_ns: int
    data: bytes | CanFrame


class _Observer:
    def receive(self, frame: CanFrame) -> None:
        # The Fabric log retains the original source timestamp; receive()
        # instead supplies delivery time and must not synthesize an echo.
        pass


@dataclass
class _CanPort:
    bus: CanBus
    qemu: QemuCanEndpoint
    observer: _Observer
    receive: Callable
    flush: Callable


def _methods(stream, names: tuple[str, ...], label: str) -> dict:
    methods = {name: getattr(stream, name, None) for name in names}
    if any(not callable(method) for method in methods.values()):
        raise TypeError(f"{label} requires callable {'/'.join(names)}")
    return methods


def _mapping(value, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an endpoint dictionary")
    if any(not isinstance(name, str) or not name for name in value):
        raise ValueError(f"{label} endpoint names must be nonempty strings")
    return dict(value)


def _integer(value, name: str, maximum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be in range 0..{maximum}")


class LabChannels:
    """Thin data-channel adapter with no resource acquisition or cleanup.

    Each ``service`` reads each stream at most once and flushes pending output.
    CAN queues/broadcast budgets retain their existing 256-frame bounds; future
    frames remain queued until the caller's QEMU-time upper bound reaches them.
    Outgoing host CAN frames cannot predate the latest service time. Observed
    QEMU frames may arrive late and retain their original wire timestamps.

    ADC1 input uses v1 co-sim RESET admission and strictly increasing timestamps,
    not STEP or a physics clock. After a received QEMU RESET, callers must
    explicitly reset again. Drain the connection's opening marker before the
    initial reset. Logical reset preserves physical RX/TX tails and does not
    provide historical CAN/v1 input isolation. Transport failures propagate;
    already delivered bytes/events are not rolled back.
    """

    def __init__(self, *, uarts: dict | None = None, cans: dict | None = None,
                 cosim=None):
        uarts = _mapping(uarts, "UART")
        cans = _mapping(cans, "CAN")
        if uarts.keys() & cans.keys():
            raise ValueError("UART and CAN endpoint names must be distinct")
        resources = [*uarts.values(), *cans.values()]
        if cosim is not None:
            resources.append(cosim)
        if len({id(stream) for stream in resources}) != len(resources):
            raise ValueError("each endpoint must borrow a distinct transport")
        sockets = [getattr(stream, "sock", None) for stream in resources]
        if len({id(sock) for sock in sockets}) != len(sockets):
            raise ValueError("each endpoint must borrow a distinct socket")

        # Complete admission before attaching any Fabric endpoint or doing IO.
        uart_methods = {}
        for name, stream in uarts.items():
            if (not isinstance(stream, StreamTransport) or
                    isinstance(stream, (FixedRecordStream, LengthPrefixedStream))):
                raise TypeError("UART requires an unframed StreamTransport")
            if not isinstance(stream.buffer, bytearray):
                raise TypeError("UART buffer must be a bytearray")
            uart_methods[name] = _methods(
                stream, ("read_once", "send", "flush"), f"UART {name}")
        can_methods = {}
        for name, stream in cans.items():
            if (not isinstance(stream, FixedRecordStream) or
                    stream.record_size != CAN_WIRE_SIZE):
                raise TypeError("CAN requires the existing fixed84 CanStream")
            can_methods[name] = _methods(
                stream, ("receive", "send", "flush"), f"CAN {name}")
        cosim_methods = None
        if cosim is not None:
            if (not isinstance(cosim, LengthPrefixedStream) or
                    (cosim.min_length, cosim.max_length) !=
                    (HEADER, HEADER + MAX_PAYLOAD)):
                raise TypeError("ADC requires the existing v1 FramedSocket")
            cosim_methods = _methods(
                cosim, ("receive", "send", "flush"), "ADC co-sim")

        self._uarts = uarts
        self._uart_methods = uart_methods
        self._cosim = cosim_methods
        self._time_ns = 0
        self._adc_sequence = 0
        self._adc_time_ns: int | None = None
        self._batch: list[ChannelEvent] | None = None
        self._cans = {}
        for name, stream in cans.items():
            bus = CanBus(log=lambda delivery, name=name:
                         self._observe_can(name, delivery))
            qemu = QemuCanEndpoint(bus, stream, "qemu")
            qemu.bind_mapper(QemuTimestampMapper(0, 0))
            observer = _Observer()
            bus.attach(observer, "lab")
            self._cans[name] = _CanPort(
                bus, qemu, observer, can_methods[name]["receive"],
                can_methods[name]["flush"])

    def _observe_can(self, endpoint: str, delivery: CanDelivery) -> None:
        if delivery.source == "qemu" and delivery.destination == "lab":
            assert self._batch is not None
            self._batch.append(ChannelEvent(
                endpoint, delivery.timestamp_ns, delivery.frame))

    def reset(self, timestamp_ns: int) -> None:
        """Reset logical queues and explicitly admit a v1 ADC session."""
        check_can_time(timestamp_ns)
        if self._cosim is not None:
            self._cosim["send"](protocol_frame(RESET, 0, timestamp_ns))
        for port in self._cans.values():
            port.bus.reset(timestamp_ns)
        self._time_ns = timestamp_ns
        self._adc_sequence = 1
        self._adc_time_ns = timestamp_ns

    def send_uart(self, endpoint: str, data: bytes) -> None:
        if endpoint not in self._uarts:
            raise ValueError(f"unknown UART endpoint: {endpoint}")
        if not isinstance(data, bytes):
            raise TypeError("UART data must be bytes")
        if len(data) > 4096:
            raise ValueError("UART data must be at most 4096 bytes")
        if data:
            self._uart_methods[endpoint]["send"](data)

    def send_can(self, endpoint: str, frame: CanFrame) -> None:
        if endpoint not in self._cans:
            raise ValueError(f"unknown CAN endpoint: {endpoint}")
        if not isinstance(frame, CanFrame):
            raise TypeError("CAN data must be a CanFrame")
        check_can_time(frame.timestamp_ns, self._time_ns)
        port = self._cans[endpoint]
        port.bus.send(port.observer, frame)

    def send_adc(self, channel: int, *, raw: int | None = None,
                 voltage_uv: int | None = None, timestamp_ns: int) -> None:
        """Inject ADC1 raw16 or 0..3.3 V through the canonical v1 codec."""
        _integer(channel, "ADC1 channel", 31)
        if (raw is None) == (voltage_uv is None):
            raise ValueError("exactly one of ADC raw or voltage_uv is required")
        if raw is not None:
            _integer(raw, "ADC raw", 0xffff)
        else:
            _integer(voltage_uv, "ADC voltage_uv", ADC_VOLTAGE_MAX_UV)
        check_can_time(timestamp_ns, self._time_ns)
        if self._cosim is None:
            raise RuntimeError("ADC co-sim is not configured")
        if self._adc_time_ns is None:
            raise RuntimeError("ADC requires an explicit reset")
        if timestamp_ns <= self._adc_time_ns:
            raise ValueError("ADC timestamp must strictly increase after RESET/input")
        _integer(self._adc_sequence, "ADC sequence", 0xffffffffffffffff)
        if raw is not None:
            frame = adc_input_frame(
                self._adc_sequence, timestamp_ns, channel, raw)
        else:
            frame = adc_voltage_frame(
                self._adc_sequence, timestamp_ns, channel, voltage_uv)
        self._cosim["send"](frame)
        self._adc_sequence += 1
        self._adc_time_ns = timestamp_ns

    def service(self, timestamp_ns: int, *, flush_uart: bool = True) -> list[ChannelEvent]:
        """Read bounded batches; an external UART TX owner may disable flushing."""
        if type(flush_uart) is not bool:
            raise TypeError("flush_uart must be a boolean")
        check_can_time(timestamp_ns, self._time_ns)
        self._time_ns = timestamp_ns
        batch = []
        self._batch = batch
        try:
            for name, stream in self._uarts.items():
                methods = self._uart_methods[name]
                methods["read_once"]()
                if stream.buffer:
                    batch.append(ChannelEvent(
                        name, timestamp_ns, bytes(stream.buffer)))
                    stream.buffer.clear()
                if flush_uart:
                    methods["flush"]()
            for port in self._cans.values():
                for can_id, flags, dlc, time_ns, data in port.receive():
                    port.qemu.ingest(
                        CanFrame(can_id, flags, dlc, data, time_ns), timestamp_ns)
                port.bus.step(timestamp_ns)
                port.flush()
            if self._cosim is not None:
                for kind, sequence, time_ns, payload in self._cosim["receive"]():
                    if kind == RESET:
                        self._adc_time_ns = None
                self._cosim["flush"]()
            return batch
        finally:
            self._batch = None
