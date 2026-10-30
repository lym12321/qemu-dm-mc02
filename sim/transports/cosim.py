"""Borrowed co-sim framing, v2 exchange and QEMU timestamp mapping.

The caller owns each socket, validator and execution lifecycle. These helpers
retain the existing bounded queues and physical tails across logical reset;
they never reset or advance a plant, CAN bus or QEMU process.
"""

from __future__ import annotations

from collections import deque
import select
import socket
import struct
import time
from typing import Callable, Iterable

from .stream import LengthPrefixedStream
from . import step_codec as step_protocol


MAGIC = 0x32434D44
VERSION = 1
HEADER = 28
MAX_PAYLOAD = 128
RESET = 1
IMU_SAMPLE = 2
TELEMETRY = 3
ADC_INPUT = 5
ADC_VOLTAGE = 6
ADC_VOLTAGE_MAX_UV = 3_300_000
ADC_VOLTAGE_FLAGS_MASK = 1
MAX_PENDING_FRAMES = 256
V2_BODY_FIXED_SIZE = 4 + step_protocol.STEP_HEADER_SIZE
V2_STEP_RESPONSE_TIMEOUT_SEC = 2.0
V2_STEP_ACK_TIMEOUT_SEC = 0.25
V2_STEP_MAX_RETRIES = 3
V2_STEP_DONE_MAX_RETRIES = 3
V2_QUEUE_FULL_RETRY_SEC = 0.001


def protocol_frame(kind: int, sequence: int, virtual_time_ns: int,
                   payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload is too large")
    body = struct.pack("<IHHIQQ", MAGIC, VERSION, kind, len(payload),
                       sequence, virtual_time_ns) + payload
    return struct.pack("<I", len(body)) + body


def adc_input_frame(sequence: int, virtual_time_ns: int,
                    channel: int, raw: int) -> bytes:
    """Build a version-1 ADC input frame for a 0..31 ADC channel."""
    if not 0 <= channel <= 31:
        raise ValueError("ADC channel must be in range 0..31")
    if not 0 <= raw <= 0xffff:
        raise ValueError("ADC raw value must be in range 0..65535")
    return protocol_frame(ADC_INPUT, sequence, virtual_time_ns,
                          struct.pack("<HHI", channel, raw, 0))


def adc_voltage_frame(sequence: int, virtual_time_ns: int,
                      channel: int, voltage_uv: int, flags: int = 0) -> bytes:
    """Build a version-1 ADC analog voltage frame (0..3.3 V)."""
    if not 0 <= channel <= 31:
        raise ValueError("ADC channel must be in range 0..31")
    if flags & ~ADC_VOLTAGE_FLAGS_MASK:
        raise ValueError("unsupported ADC voltage flags")
    if not 0 <= voltage_uv <= ADC_VOLTAGE_MAX_UV:
        raise ValueError("ADC voltage must be in range 0..3.3 V")
    return protocol_frame(ADC_VOLTAGE, sequence, virtual_time_ns,
                          struct.pack("<HHII", channel, flags,
                                      voltage_uv, 0))


class QemuTimestampMapper:
    """Translate between QEMU virtual time and the worker's step epoch."""

    def __init__(self, qemu_origin_ns: int, worker_origin_ns: int):
        self.qemu_origin_ns = qemu_origin_ns
        self.worker_origin_ns = worker_origin_ns

    def map(self, qemu_timestamp_ns: int, fallback_ns: int) -> int:
        if qemu_timestamp_ns < self.qemu_origin_ns:
            return fallback_ns
        return self.worker_origin_ns + (
            qemu_timestamp_ns - self.qemu_origin_ns)

    def unmap(self, worker_timestamp_ns: int, fallback_ns: int) -> int:
        if worker_timestamp_ns < self.worker_origin_ns:
            return fallback_ns
        return self.qemu_origin_ns + (
            worker_timestamp_ns - self.worker_origin_ns)



class V2SessionReset(RuntimeError):
    """QEMU started a new v2 session while a step was in flight."""

    def __init__(self, step_id: int, t_sim_ns: int,
                 session_id: int = 0) -> None:
        super().__init__(
            f"QEMU reset v2 session at step {step_id}, time {t_sim_ns}, "
            f"session {session_id}")
        self.step_id = step_id
        self.t_sim_ns = t_sim_ns
        self.session_id = session_id



def validate_v2_response(frame: step_protocol.StepFrame, validator
                         ) -> (step_protocol.ResetAckPayload |
                               step_protocol.StepAckPayload |
                               step_protocol.StepDonePayload | None):
    """Validate one response using the canonical session and payload codecs."""
    validator.accept(frame)
    if frame.header.kind == step_protocol.RESET:
        return None
    if frame.header.kind == step_protocol.RESET_ACK:
        try:
            ack = step_protocol.decode_reset_ack_payload(frame.payload)
        except step_protocol.StepProtocolError as exc:
            raise RuntimeError("invalid RESET_ACK payload") from exc
    elif frame.header.kind == step_protocol.STEP_ACK:
        try:
            ack = step_protocol.decode_step_ack_payload(frame.payload)
        except step_protocol.StepProtocolError as exc:
            raise RuntimeError("invalid STEP_ACK payload") from exc
    elif frame.header.kind == step_protocol.STEP_DONE:
        try:
            ack = step_protocol.decode_step_done_payload(frame.payload)
        except step_protocol.StepProtocolError as exc:
            raise RuntimeError("invalid STEP_DONE payload") from exc
    elif frame.header.kind == step_protocol.DIAGNOSTICS:
        try:
            step_protocol.decode_diagnostics_payload(frame.payload)
        except step_protocol.StepProtocolError as exc:
            raise RuntimeError("invalid diagnostics payload") from exc
        ack = None
    elif frame.header.kind == step_protocol.TELEMETRY:
        try:
            step_protocol.decode_board_telemetry_payload(frame.payload)
        except step_protocol.StepProtocolError as exc:
            raise RuntimeError("invalid board telemetry payload") from exc
        ack = None
    elif frame.header.kind == step_protocol.MOTOR_STATE:
        try:
            step_protocol.decode_motor_state(frame.payload)
        except (ValueError, struct.error) as exc:
            raise RuntimeError("invalid motor state payload") from exc
        ack = None
    else:
        ack = None
    return ack


def poll_v2_response(stream: StepFramedSocket, validator):
    """Consume one available head without waiting or flushing borrowed IO.

    Return ``(frame, payload)`` or ``None`` when no frame is available. RESET
    is returned explicitly; its execution policy belongs to the caller. The
    decoded suffix is returned before validating the head, including on error.
    """
    received = stream.receive()
    if not received:
        return None
    stream.push_front(received[1:])
    frame = received[0]
    return frame, validate_v2_response(frame, validator)


def wait_v2_response(stream: StepFramedSocket, validator,
                     expected_kind: int, expected_step_id: int,
                     expected_time_ns: int, timeout: float = 2.0,
                     allow_queue_full: bool = False,
                     service_io: Callable[[float], None] | None = None,
                     interrupt_on_reset: bool = False):
    """Wait for one response while validating the v2 response session."""
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for v2 response")
        if service_io is not None:
            service_io(0)
        elif stream.tx_pending:
            stream.flush()
        received = stream.receive()
        for frame_index, frame in enumerate(received):
            ack = validate_v2_response(frame, validator)
            if frame.header.kind == step_protocol.RESET:
                if (expected_kind != step_protocol.RESET_ACK or
                        interrupt_on_reset):
                    # Stop replaying the old STEP as soon as RESET starts,
                    # even if RESET_ACK arrives in a later socket read.
                    stream.push_front(received[frame_index + 1:])
                    raise V2SessionReset(frame.header.step_id,
                                         frame.header.t_sim_ns,
                                         frame.header.session_id)
                continue
            if frame.header.kind != expected_kind:
                if frame.header.kind == step_protocol.DIAGNOSTICS:
                    continue
                if frame.header.kind == step_protocol.TELEMETRY:
                    continue
                if frame.header.kind == step_protocol.MOTOR_STATE:
                    continue
                if (frame.header.kind == step_protocol.STEP_DONE and
                        expected_kind == step_protocol.STEP_ACK and
                        not validator.require_step_done):
                    # In compatibility mode DONE is advisory. It may arrive
                    # after the ACK wait returned, including in a later
                    # socket read, so consume it without changing pacing.
                    continue
                if (frame.header.kind == step_protocol.STEP_ACK and
                        expected_kind == step_protocol.STEP_DONE and
                        ack is not None and
                        ack.status == step_protocol.STATUS_OK):
                    # A STEP replay used to recover a lost DONE also produces
                    # an idempotent ACK. It does not satisfy the consumption
                    # barrier, so keep waiting for DONE.
                    continue
                raise RuntimeError(
                    f"unexpected v2 response kind {frame.header.kind}")
            if (frame.header.step_id != expected_step_id or
                    frame.header.t_sim_ns != expected_time_ns):
                raise RuntimeError("v2 response timestamp or step id mismatch")
            status = ack.status if ack is not None else None
            if status is None:
                raise RuntimeError("v2 response has no status")
            if status == step_protocol.STATUS_QUEUE_FULL and allow_queue_full:
                stream.push_front(received[frame_index + 1:])
                return frame
            if status != 0:
                if frame.header.kind == step_protocol.STEP_DONE:
                    raise RuntimeError(
                        f"STEP_DONE failed: step={frame.header.step_id} "
                        f"status={status} consumed={ack.consumed_mask:#x} "
                        f"missing={ack.missing_mask:#x}")
                raise RuntimeError(f"v2 response failed with status {status}")
            # One socket read may contain ACK and its later DONE. Preserve
            # frames after the response requested by this call so the next
            # wait observes them in wire order instead of silently dropping
            # a valid second-phase response.
            stream.push_front(received[frame_index + 1:])
            return frame
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for v2 response")
        if service_io is not None:
            service_io(remaining)
        else:
            writable = [stream.sock] if stream.tx_pending else []
            readable, writable_ready, _ = select.select(
                [stream.sock], writable, [], remaining)
            if writable_ready:
                stream.flush()
            if not readable and not writable_ready:
                raise TimeoutError("timed out waiting for v2 response")



def send_v2_step(stream: StepFramedSocket, validator, step_wire: bytes,
                 step_id: int, timestamp_ns: int,
                 service_io: Callable[[float], None] | None = None):
    """Send one STEP and replay its bytes within one admission deadline.

    QUEUE_FULL waits briefly for recovery while servicing transport, without
    renewing the budget or stepping the backend again. Silent ACK timeouts
    retain the existing retry limit. Strict consumption is a separate phase.
    """
    deadline = time.monotonic() + V2_STEP_RESPONSE_TIMEOUT_SEC
    ack_retries = 0
    retry_at = None
    last_status = None
    queue_depth = None
    stream.send(step_wire)

    def timed_out():
        return TimeoutError(
            f"STEP_ACK admission timed out: step={step_id} "
            f"time={timestamp_ns} last_status={last_status} "
            f"queue_depth={queue_depth} retries={ack_retries}")

    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise timed_out()
        if retry_at is not None and now >= retry_at:
            if not stream.tx_pending:
                stream.send(step_wire)
            retry_at = None
        wait_time = min(V2_STEP_ACK_TIMEOUT_SEC, remaining)
        if retry_at is not None:
            wait_time = min(wait_time, retry_at - now)
        try:
            response = wait_v2_response(
                stream, validator, step_protocol.STEP_ACK,
                step_id, timestamp_ns, timeout=wait_time,
                allow_queue_full=True, service_io=service_io)
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise timed_out() from None
            if retry_at is None:
                if ack_retries >= V2_STEP_MAX_RETRIES:
                    raise timed_out() from None
                ack_retries += 1
            retry_at = None
            # A blocked or partly written STEP is still in the bounded TX
            # queue. Flush it during the next wait; do not append a copy.
            if not stream.tx_pending:
                stream.send(step_wire)
            continue
        ack = step_protocol.decode_step_ack_payload(response.payload)
        last_status, queue_depth = ack.status, ack.queue_depth
        if time.monotonic() >= deadline:
            raise timed_out()
        if last_status != step_protocol.STATUS_QUEUE_FULL:
            return response
        if retry_at is None:
            retry_at = min(deadline,
                           time.monotonic() + V2_QUEUE_FULL_RETRY_SEC)



class FramedSocket(LengthPrefixedStream):
    """Legacy codec over the shared bounded, length-prefixed transport."""

    def __init__(self, sock: socket.socket):
        super().__init__(
            sock, min_length=HEADER, max_length=HEADER + MAX_PAYLOAD,
            length_error="invalid co-sim frame length",
            max_pending=MAX_PENDING_FRAMES, queue_name="co-sim",
            endpoint_name="QEMU co-sim", writable_name="co-sim",
            clock=time, poller=select)

    def receive(self) -> list[tuple[int, int, int, bytes]]:
        frames = []
        for raw in self.receive_records():
            body = raw[4:]
            length = len(body)
            magic, version, kind, payload_len, seq, virtual_time_ns = \
                struct.unpack_from("<IHHIQQ", body)
            if (magic, version, payload_len) != (MAGIC, VERSION, length - HEADER):
                raise ValueError("invalid co-sim frame header")
            frames.append((kind, seq, virtual_time_ns, body[HEADER:]))
        return frames



class StepFramedSocket(FramedSocket):
    """Length-prefixed v2 control stream sharing the QEMU chardev.

    QEMU emits one legacy v1 telemetry snapshot when a chardev opens.  A v2
    client discards that compatibility snapshot and retains native v2
    telemetry while consuming control responses.  The outbound queue and
    partial-write behavior remain identical to :class:`FramedSocket`.
    """

    def __init__(self, sock: socket.socket):
        super().__init__(sock)
        self.max_length = (4 + step_protocol.STEP_HEADER_SIZE +
                           step_protocol.STEP_MAX_PAYLOAD)
        self.length_error = "invalid co-sim v2 frame length"
        self.pending_frames: deque[step_protocol.StepFrame] = deque()

    def receive(self) -> list[step_protocol.StepFrame]:
        if self.pending_frames:
            # Finish the previous decoded batch before reading more bytes.
            # ACK waits may return its suffix here for the DONE/reset wait.
            frames = list(self.pending_frames)
            self.pending_frames.clear()
            return frames
        frames: list[step_protocol.StepFrame] = []
        for raw in self.receive_records():
            body_len = len(raw) - 4
            if len(raw) < 8 or struct.unpack_from("<I", raw, 4)[0] != MAGIC:
                raise ValueError("invalid co-sim v2 frame magic")
            version = struct.unpack_from("<H", raw, 8)[0]
            if version == VERSION:
                # The open notification is a v1 telemetry snapshot. Validate
                # its fixed shape before ignoring it in v2 mode.
                if body_len < HEADER or body_len > HEADER + MAX_PAYLOAD:
                    raise ValueError("invalid legacy co-sim frame length")
                payload_len = struct.unpack_from("<I", raw, 12)[0]
                if body_len != HEADER + payload_len:
                    raise ValueError("invalid legacy co-sim frame header")
                continue
            if version != step_protocol.STEP_VERSION:
                raise ValueError("unsupported co-sim v2 frame version")
            frames.append(step_protocol.decode_step_frame(raw))
        return frames

    def push_front(self, frames: Iterable[step_protocol.StepFrame]) -> None:
        """Return already-decoded frames to the front of the receive queue."""
        for frame in reversed(tuple(frames)):
            self.pending_frames.appendleft(frame)



def wait_v2_reset_ack(stream: StepFramedSocket, validator,
                      reset: V2SessionReset,
                      service_io: Callable[[float], None] | None = None,
                      ) -> V2SessionReset:
    """Return the latest reset metadata only after its matching ACK.

    RESET already changed the RX validator. Repeated RESET replaces the
    session but never renews this handshake's absolute two-second budget.
    Execution reset and epoch binding remain the caller's responsibility.
    """
    deadline = time.monotonic() + V2_STEP_RESPONSE_TIMEOUT_SEC
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for v2 RESET_ACK")
        try:
            wait_v2_response(
                stream, validator, step_protocol.RESET_ACK,
                reset.step_id, reset.t_sim_ns, timeout=remaining,
                service_io=service_io, interrupt_on_reset=True)
        except V2SessionReset as latest_reset:
            reset = latest_reset
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for v2 RESET_ACK")
        return reset
