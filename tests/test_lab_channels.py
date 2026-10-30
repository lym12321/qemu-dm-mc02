"""Literal wire/socketpair consumers of borrowed Lab data channels."""

from contextlib import contextmanager
from pathlib import Path
import socket
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.boards.channels import ChannelEvent, LabChannels
from sim.fabric import CanBus, CanFrame
from sim.fabric.frame import WIRE_FD, WIRE_BRS
from sim.transports import StreamTransport
from sim.transports.can_stream import CanStream
from sim.transports.cosim import FramedSocket, StepFramedSocket


@contextmanager
def pair(wrapper=StreamTransport):
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.setblocking(False)
    try:
        yield wrapper(local), peer
    finally:
        local.close()
        peer.close()


def can_wire(can_id, flags, dlc, timestamp, data):
    # Existing public fixed84 schema, independent of the production codec.
    return struct.pack("<IIB3xQ64s", can_id, flags, dlc, timestamp, data)


def adc_wire(kind, sequence, timestamp, payload=b""):
    # Existing v1 wire schema, independent of worker.protocol_frame.
    body = struct.pack("<IHHIQQ", 0x32434d44, 1, kind, len(payload),
                       sequence, timestamp) + payload
    return struct.pack("<I", len(body)) + body


def no_bytes(peer):
    with pytest.raises(BlockingIOError):
        peer.recv(4096)


def test_uart_one_read_bounded_bytes_and_caller_lifetime():
    with pair() as (stream, peer):
        lab = LabChannels(uarts={"usart3": stream})
        peer.sendall(b"a" * 4097)
        assert lab.service(7) == [ChannelEvent("usart3", 7, b"a" * 4096)]
        assert lab.service(8) == [ChannelEvent("usart3", 8, b"a")]
        assert lab.service(8) == [] and stream.buffer == bytearray()
        lab.send_uart("usart3", b"\0\xff\r\n")
        assert peer.recv(4096) == b"\0\xff\r\n"
        lab.send_uart("usart3", b"")
        no_bytes(peer)
        lab.reset(9)
        assert stream.sock.fileno() >= 0
        assert not hasattr(lab, "close")


class ShortSocket:
    """Deterministic partial writes over a real, borrowed socketpair."""

    def __init__(self, sock):
        self.sock = sock

    def recv(self, size):
        return self.sock.recv(size)

    def send(self, data):
        return self.sock.send(data[:2])


def test_uart_partial_send_suffix_survives_logical_reset():
    with pair() as (original, peer):
        stream = StreamTransport(ShortSocket(original.sock))
        lab = LabChannels(uarts={"uart1": stream})
        lab.send_uart("uart1", b"abcde")
        assert bytes(stream.tx_queue[0]) == b"cde"
        lab.reset(10)
        assert bytes(stream.tx_queue[0]) == b"cde"
        peer.sendall(b"READY\0")
        assert lab.service(10, flush_uart=False) == [ChannelEvent("uart1", 10, b"READY\0")]
        assert bytes(stream.tx_queue[0]) == b"cde" and peer.recv(4096) == b"ab"
        assert lab.service(10) == []
        assert bytes(stream.tx_queue[0]) == b"e"
        assert lab.service(11) == [] and not stream.tx_pending
        assert peer.recv(4096) == b"cde"


@pytest.mark.parametrize("flag", [0, None, "false"])
def test_uart_flush_option_rejects_before_read_or_time_commit(flag):
    with pair() as (stream, peer):
        lab = LabChannels(uarts={"uart7": stream})
        peer.sendall(b"ready")
        with pytest.raises(TypeError, match="flush_uart"):
            lab.service(99, flush_uart=flag)
        assert lab.service(1, flush_uart=False) == [ChannelEvent("uart7", 1, b"ready")]
        no_bytes(peer)


def test_referee_tx_owner_retains_suffix_while_lab_observes_same_uart():
    from sim.devices.referee import RefereeUartEndpoint
    from test_referee_endpoint import SendBudget, STATE, WIRE, received

    with pair() as (original, peer):
        budget = SendBudget(original.sock)
        stream = StreamTransport(budget)
        endpoint = RefereeUartEndpoint(stream, sequence=254)
        lab = LabChannels(uarts={"uart7": stream})
        try:
            budget.budget = 19
            assert endpoint.send_state(STATE) == WIRE
            suffix = stream.tx_queue[0]
            peer.sendall(b"READY")
            assert lab.service(7, flush_uart=False) == [ChannelEvent("uart7", 7, b"READY")]
            assert budget.sends == budget.reads == 1
            assert stream.tx_queue[0] is suffix and bytes(suffix) == WIRE[19:]
            budget.budget = None
            assert endpoint.transport.flush() and received(peer) == WIRE
        finally:
            endpoint.close()
        assert original.sock.fileno() >= 0 and budget.closes == 0


@pytest.mark.parametrize("flags,dlc,payload", [
    (0, 8, b"abcdefgh"),
    (WIRE_FD | WIRE_BRS, 15, bytes(range(64))),
])
def test_can_fixed84_both_directions_without_synthetic_echo(flags, dlc, payload):
    with pair(CanStream) as (stream, peer):
        lab = LabChannels(cans={"fdcan1": stream})
        raw = can_wire(0x123, flags, dlc, 5, payload)
        peer.sendall(raw[:23])
        assert lab.service(7) == []
        lab.reset(8)
        assert bytes(stream.buffer) == raw[:23]
        peer.sendall(raw[23:])
        observed = CanFrame(0x123, flags, dlc, payload, 5)
        assert lab.service(9) == [ChannelEvent("fdcan1", 5, observed)]
        no_bytes(peer)  # Observing a guest TX frame cannot echo it back.
        sent = CanFrame(0x321, flags, dlc, payload, 10)
        lab.send_can("fdcan1", sent)
        no_bytes(peer)  # Enqueueing does not advance a bus or CPU.
        assert lab.service(9) == []
        no_bytes(peer)
        assert lab.service(10) == []
        assert peer.recv(4096) == can_wire(0x321, flags, dlc, 10, payload)
        assert stream.sock.fileno() >= 0


def test_can_future_late_and_original_qemu_timestamp():
    with pair(CanStream) as (stream, peer):
        lab = LabChannels(cans={"can": stream})
        peer.sendall(can_wire(1, 0, 1, 20, b"f"))
        assert lab.service(10) == []
        assert lab.service(20) == [ChannelEvent(
            "can", 20, CanFrame(1, data=b"f", timestamp_ns=20))]
        peer.sendall(can_wire(2, 0, 1, 3, b"l"))
        assert lab.service(20) == [ChannelEvent(
            "can", 3, CanFrame(2, data=b"l", timestamp_ns=3))]
        lab.send_can("can", CanFrame(3, data=b"x", timestamp_ns=21))
        assert lab.service(30) == []
        # Fabric's existing egress boundary publishes delivery time.
        assert peer.recv(84) == can_wire(3, 0, 1, 30, b"x")
        with pytest.raises(ValueError):
            lab.send_can("can", CanFrame(4, timestamp_ns=29))
        with pytest.raises(ValueError):
            lab.service(29)
        no_bytes(peer)


def test_can_capacity_is_explicit_without_unbounded_host_queue():
    with pair(CanStream) as (stream, peer):
        lab = LabChannels(cans={"can": stream})
        for _ in range(256):
            lab.send_can("can", CanFrame(1, timestamp_ns=100))
        with pytest.raises(BufferError, match="capacity") as caught:
            lab.send_can("can", CanFrame(2, timestamp_ns=100))
        with pytest.raises(BufferError) as repeated:
            lab.service(99)
        assert repeated.value is caught.value
        no_bytes(peer)
        lab.reset(100)
        assert lab.service(100) == []
        no_bytes(peer)


def test_adc_literal_reset_raw_and_voltage_then_received_reset_admission():
    with pair(FramedSocket) as (stream, peer):
        lab = LabChannels(cosim=stream)
        with pytest.raises(RuntimeError, match="explicit reset"):
            lab.send_adc(0, raw=0, timestamp_ns=1)
        no_bytes(peer)
        lab.reset(10)
        lab.send_adc(31, raw=65535, timestamp_ns=11)
        lab.send_adc(0, voltage_uv=3300000, timestamp_ns=12)
        assert peer.recv(4096) == (
            adc_wire(1, 0, 10) +
            adc_wire(5, 1, 11, struct.pack("<HHI", 31, 65535, 0)) +
            adc_wire(6, 2, 12, struct.pack("<HHII", 0, 0, 3300000, 0)))
        telemetry = adc_wire(3, 42, 13, b"telemetry")
        peer.sendall(telemetry[:7])
        assert lab.service(13) == [] and stream.buffer
        peer.sendall(telemetry[7:] + adc_wire(1, 0, 14))
        assert lab.service(14) == [] and not stream.buffer
        with pytest.raises(RuntimeError, match="explicit reset"):
            lab.send_adc(0, raw=0, timestamp_ns=15)
        lab.reset(14)
        lab.send_adc(0, raw=0, timestamp_ns=15)
        assert peer.recv(4096) == (
            adc_wire(1, 0, 14) + adc_wire(5, 1, 15, b"\0" * 8))
        assert stream.sock.fileno() >= 0


@pytest.mark.parametrize("kwargs,error", [
    ({"raw": 1, "voltage_uv": 1}, ValueError),
    ({}, ValueError),
    ({"raw": True}, TypeError),
    ({"raw": -1}, ValueError),
    ({"raw": 65536}, ValueError),
    ({"voltage_uv": 1.0}, TypeError),
    ({"voltage_uv": 3300001}, ValueError),
])
def test_adc_bad_input_has_no_transport_or_sequence_side_effect(kwargs, error):
    with pair(FramedSocket) as (stream, peer):
        lab = LabChannels(cosim=stream)
        lab.reset(10)
        peer.recv(4096)
        with pytest.raises(error):
            lab.send_adc(0, timestamp_ns=11, **kwargs)
        no_bytes(peer)
        lab.send_adc(0, raw=7, timestamp_ns=11)
        assert peer.recv(4096) == adc_wire(5, 1, 11, b"\0\0\7\0\0\0\0\0")


def test_time_channel_and_uart_admission_precede_io():
    with pair() as (uart, uart_peer), pair(FramedSocket) as (cosim, adc_peer):
        lab = LabChannels(uarts={"uart": uart}, cosim=cosim)
        lab.reset(10)
        adc_peer.recv(4096)
        for channel in (-1, 32, True):
            with pytest.raises((TypeError, ValueError)):
                lab.send_adc(channel, raw=1, timestamp_ns=11)
        for time in (10, True, -1, 2**64):
            with pytest.raises((TypeError, ValueError)):
                lab.send_adc(0, raw=1, timestamp_ns=time)
        for data in ("text", bytearray(b"abc"), b"a" * 4097):
            with pytest.raises((TypeError, ValueError)):
                lab.send_uart("uart", data)
        with pytest.raises(ValueError, match="unknown UART"):
            lab.send_uart("other", b"x")
        uart_peer.sendall(b"retained")
        with pytest.raises(ValueError):
            lab.service(9)
        assert lab.service(10) == [ChannelEvent("uart", 10, b"retained")]
        no_bytes(uart_peer)
        no_bytes(adc_peer)


def test_complete_constructor_admission_before_fabric_attachment(monkeypatch):
    attached = []
    original = CanBus.attach

    def observe(bus, endpoint, name=None):
        attached.append(name)
        return original(bus, endpoint, name)

    monkeypatch.setattr(CanBus, "attach", observe)
    with pair(CanStream) as (can, peer), pair(StepFramedSocket) as (v2, _):
        with pytest.raises(TypeError, match="v1 FramedSocket"):
            LabChannels(cans={"can": can}, cosim=v2)
        with pytest.raises(ValueError, match="distinct transport"):
            LabChannels(cans={"a": can, "b": can})
        with pytest.raises(ValueError, match="distinct socket"):
            LabChannels(cans={"a": can, "b": CanStream(can.sock)})
        with pytest.raises(TypeError, match="unframed"):
            LabChannels(uarts={"uart": can})
        with pytest.raises(ValueError, match="nonempty"):
            LabChannels(cans={"": can})
        can.flush = None
        with pytest.raises(TypeError, match="callable"):
            LabChannels(cans={"can": can})
        assert attached == []
        no_bytes(peer)
        assert can.sock.fileno() >= 0
