#!/usr/bin/env python3
"""Small DM-MC02 external simulation worker.

The worker deliberately keeps the QEMU-facing hot path binary and fixed
width.  It connects to the QEMU co-sim chardev for IMU/telemetry and, when
requested, to one FDCAN chardev for timestamped 84-byte CAN frames.  The
default NullEngine is useful for wiring tests; MuJoCo is an optional import.
"""

from __future__ import annotations

import argparse
from collections import deque
import heapq
import importlib.util
import math
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from dm_mc02_socketcan import (CanFrame, SocketCANError, open_socketcan,
                                   pack_can_frame, pack_wire_frame,
                                   unpack_can_frame, unpack_wire_frame)
except ModuleNotFoundError as exc:
    # Some smoke tests load this file by path with importlib, so its directory
    # is not necessarily present on sys.path. Keep the worker usable in that
    # mode without requiring tools/ to become a Python package.
    if exc.name != "dm_mc02_socketcan":
        raise
    socketcan_path = Path(__file__).with_name("dm_mc02_socketcan.py")
    socketcan_spec = importlib.util.spec_from_file_location(
        "dm_mc02_socketcan", socketcan_path)
    if socketcan_spec is None or socketcan_spec.loader is None:
        raise ImportError(f"cannot load {socketcan_path}") from exc
    socketcan_module = importlib.util.module_from_spec(socketcan_spec)
    sys.modules[socketcan_spec.name] = socketcan_module
    socketcan_spec.loader.exec_module(socketcan_module)
    from dm_mc02_socketcan import (CanFrame, SocketCANError, open_socketcan,
                                   pack_can_frame, pack_wire_frame,
                                   unpack_can_frame, unpack_wire_frame)

try:
    import dm_mc02_step_protocol as step_protocol
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_step_protocol":
        raise
    step_protocol_path = Path(__file__).with_name("dm_mc02_step_protocol.py")
    step_protocol_spec = importlib.util.spec_from_file_location(
        "dm_mc02_step_protocol", step_protocol_path)
    if step_protocol_spec is None or step_protocol_spec.loader is None:
        raise ImportError(f"cannot load {step_protocol_path}") from exc
    step_protocol = importlib.util.module_from_spec(step_protocol_spec)
    sys.modules[step_protocol_spec.name] = step_protocol
    step_protocol_spec.loader.exec_module(step_protocol)

try:
    from dm_mc02_motor_adapter import DmMotorBusAdapter
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_motor_adapter":
        raise
    motor_adapter_path = Path(__file__).with_name("dm_mc02_motor_adapter.py")
    motor_adapter_spec = importlib.util.spec_from_file_location(
        "dm_mc02_motor_adapter", motor_adapter_path)
    if motor_adapter_spec is None or motor_adapter_spec.loader is None:
        raise ImportError(f"cannot load {motor_adapter_path}") from exc
    motor_adapter_module = importlib.util.module_from_spec(motor_adapter_spec)
    sys.modules[motor_adapter_spec.name] = motor_adapter_module
    motor_adapter_spec.loader.exec_module(motor_adapter_module)
    from dm_mc02_motor_adapter import DmMotorBusAdapter

try:
    from dm_mc02_step_coordinator import StepCoordinator
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_step_coordinator":
        raise
    coordinator_path = Path(__file__).with_name("dm_mc02_step_coordinator.py")
    coordinator_spec = importlib.util.spec_from_file_location(
        "dm_mc02_step_coordinator", coordinator_path)
    if coordinator_spec is None or coordinator_spec.loader is None:
        raise ImportError(f"cannot load {coordinator_path}") from exc
    coordinator_module = importlib.util.module_from_spec(coordinator_spec)
    sys.modules[coordinator_spec.name] = coordinator_module
    coordinator_spec.loader.exec_module(coordinator_module)
    from dm_mc02_step_coordinator import StepCoordinator

try:
    from dm_mc02_backend_registry import (BackendRegistry, load_factory,
                                          load_registry)
except ModuleNotFoundError as exc:
    if exc.name != "dm_mc02_backend_registry":
        raise
    backend_registry_path = Path(__file__).with_name(
        "dm_mc02_backend_registry.py")
    backend_registry_spec = importlib.util.spec_from_file_location(
        "dm_mc02_backend_registry", backend_registry_path)
    if (backend_registry_spec is None or
            backend_registry_spec.loader is None):
        raise ImportError(f"cannot load {backend_registry_path}") from exc
    backend_registry_module = importlib.util.module_from_spec(
        backend_registry_spec)
    sys.modules[backend_registry_spec.name] = backend_registry_module
    backend_registry_spec.loader.exec_module(backend_registry_module)
    from dm_mc02_backend_registry import (BackendRegistry, load_factory,
                                          load_registry)

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
CAN_WIRE_SIZE = 84
CAN_FLAG_RTR = 1 << 1
CAN_FLAG_FD = 1 << 2
MAX_PENDING_FRAMES = 256
V2_BODY_FIXED_SIZE = 4 + step_protocol.STEP_HEADER_SIZE
ROS_RESET_TIMEOUT_SEC = 2.0
V2_STEP_RESPONSE_TIMEOUT_SEC = 2.0
V2_STEP_ACK_TIMEOUT_SEC = 0.25
V2_STEP_MAX_RETRIES = 3
V2_STEP_DONE_MAX_RETRIES = 3

# The DM motor used by the board firmware speaks the common MIT 8-byte CAN
# protocol.  Keep the generic float command path below for small plant smoke
# tests, but do not silently interpret a real MIT packet as a float.
DM_RESET_COMMAND = b"\xff\xff\xff\xff\xff\xff\xff\xfb"
DM_ENABLE_COMMAND = b"\xff\xff\xff\xff\xff\xff\xff\xfc"
DM_DISABLE_COMMAND = b"\xff\xff\xff\xff\xff\xff\xff\xfd"
DM_DEFAULT_SLAVE_ID = 1
DM_DEFAULT_FEEDBACK_ID = 0x11
DM_DEFAULT_P_MAX = 12.5
DM_DEFAULT_V_MAX = 30.0
DM_DEFAULT_T_MAX = 10.0


def realtime_target_ns(wall_origin_ns: int, virtual_time_ns: int,
                       clock_base_ns: int) -> int:
    """Map a virtual timestamp onto the wall-clock origin of this worker."""
    return wall_origin_ns + virtual_time_ns - clock_base_ns


class ExternalTimestampMapper:
    """Map an external sensor clock into the worker's monotonic clock.

    External engines may repeat a message while the worker advances, or may
    publish a timestamp that moves backwards after a simulator reset.  Keep
    the useful part of a valid forward-moving clock without allowing the
    protocol timestamp to move backwards.
    """

    def __init__(self, clock_base_ns: int):
        self.clock_base_ns = clock_base_ns
        self.origin_ns: int | None = None
        self.mapped_origin_ns: int | None = None
        self.last_external_ns: int | None = None
        self.last_mapped_ns = clock_base_ns
        self.external_stalled = False
        self.external_recovered = False

    def map(self, external_ns: int | None, fallback_ns: int) -> int:
        fallback = max(fallback_ns, self.last_mapped_ns + 1)
        candidate = fallback
        if external_ns is not None and external_ns >= 0:
            if self.origin_ns is None:
                self.origin_ns = external_ns
                self.mapped_origin_ns = self.clock_base_ns
                self.last_external_ns = external_ns
            elif external_ns >= self.last_external_ns:
                if external_ns == self.last_external_ns:
                    self.external_stalled = True
                else:
                    assert self.mapped_origin_ns is not None
                    if self.external_stalled:
                        # The fallback clock may have advanced far beyond the
                        # old mapping while ROS/Gazebo was paused or reset.
                        # Anchor the first forward sample at the current
                        # monotonic floor, then let subsequent external
                        # deltas control the output again.
                        self.origin_ns = external_ns
                        self.mapped_origin_ns = fallback
                        self.external_stalled = False
                        self.external_recovered = True
                    candidate = self.mapped_origin_ns + (
                        external_ns - self.origin_ns)
                    self.last_external_ns = external_ns
            else:
                # ROS/Gazebo reset starts a new external clock epoch. Keep the
                # fallback moving until the new epoch produces a forward
                # sample; that sample is re-anchored in the branch above.
                self.origin_ns = external_ns
                self.last_external_ns = external_ns
                self.external_stalled = True
                self.external_recovered = False
        elif self.origin_ns is not None:
            self.external_stalled = True
            self.external_recovered = False

        if (external_ns is not None and external_ns >= 0 and
                not self.external_stalled and not self.external_recovered):
            # During a healthy external session the fallback is still a floor
            # for the first samples, but it must not permanently mask a
            # recovered external clock.
            candidate = max(candidate, fallback)
        candidate = max(candidate, self.last_mapped_ns + 1)
        self.last_mapped_ns = candidate
        return candidate


class QemuTimestampMapper:
    """Translate QEMU virtual timestamps into the worker's step epoch."""

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


def protocol_frame(kind: int, sequence: int, virtual_time_ns: int,
                   payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload is too large")
    body = struct.pack("<IHHIQQ", MAGIC, VERSION, kind, len(payload),
                       sequence, virtual_time_ns) + payload
    return struct.pack("<I", len(body)) + body


def imu_frame(sequence: int, virtual_time_ns: int,
              gyro: Iterable[float], accel: Iterable[float]) -> bytes:
    return protocol_frame(IMU_SAMPLE, sequence, virtual_time_ns,
                          struct.pack("<6f", *gyro, *accel))


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


def wait_v2_response(stream: StepFramedSocket, validator,
                     expected_kind: int, expected_step_id: int,
                     expected_time_ns: int, timeout: float = 2.0,
                     allow_queue_full: bool = False):
    """Wait for one response while validating the v2 response session."""
    deadline = time.monotonic() + timeout
    async_reset = False
    while True:
        received = stream.receive()
        for frame_index, frame in enumerate(received):
            validator.accept(frame)
            if frame.header.kind == step_protocol.RESET:
                async_reset = True
                continue
            if frame.header.kind == step_protocol.RESET_ACK:
                try:
                    ack = step_protocol.decode_reset_ack_payload(frame.payload)
                except step_protocol.StepProtocolError as exc:
                    raise RuntimeError("invalid RESET_ACK payload") from exc
                if async_reset and expected_kind != step_protocol.RESET_ACK:
                    # RESET_ACK closes the unsolicited reset handshake. The
                    # in-flight STEP was invalidated by that reset and must be
                    # rebuilt against the new plant/session epoch.
                    raise V2SessionReset(frame.header.step_id,
                                         frame.header.t_sim_ns,
                                         frame.header.session_id)
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
        readable, _, _ = select.select([stream.sock], [], [], remaining)
        if not readable:
            raise TimeoutError("timed out waiting for v2 response")


class FramedSocket:
    """Non-blocking parser and bounded TX queue for the framed stream.

    The worker is the producer of the simulation stream.  A slow QEMU
    chardev must therefore apply backpressure to the worker without making a
    blocking ``send`` call in the simulation loop.  Frames remain intact and
    ordered; a persistently stalled peer fails with a bounded-queue error
    instead of silently losing samples.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = bytearray()
        self.tx_queue: deque[memoryview] = deque()

    def send(self, data: bytes) -> None:
        if len(self.tx_queue) >= MAX_PENDING_FRAMES:
            self.flush()
        if len(self.tx_queue) >= MAX_PENDING_FRAMES:
            raise BufferError("co-sim TX queue is full")
        self.tx_queue.append(memoryview(data))
        self.flush()

    @property
    def tx_pending(self) -> bool:
        return bool(self.tx_queue)

    def flush(self) -> bool:
        """Write as much as possible without waiting for socket writability."""
        while self.tx_queue:
            view = self.tx_queue[0]
            try:
                written = self.sock.send(view)
            except BlockingIOError:
                return False
            if written == 0:
                raise ConnectionError("QEMU co-sim socket closed")
            if written < len(view):
                self.tx_queue[0] = view[written:]
                return False
            self.tx_queue.popleft()
        return True

    def drain(self, timeout: float = 1.0) -> None:
        """Drain queued frames during orderly worker shutdown."""
        deadline = time.monotonic() + timeout
        while not self.flush():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("co-sim TX queue did not drain")
            _, writable, _ = select.select([], [self.sock], [], remaining)
            if not writable:
                raise TimeoutError("co-sim socket is not writable")

    def receive(self) -> list[tuple[int, int, int, bytes]]:
        frames = []
        while True:
            try:
                data = self.sock.recv(4096)
            except BlockingIOError:
                break
            if not data:
                raise ConnectionError("QEMU co-sim socket closed")
            self.buffer.extend(data)
        while len(self.buffer) >= 4:
            length = struct.unpack_from("<I", self.buffer)[0]
            if length < HEADER or length > HEADER + MAX_PAYLOAD:
                raise ValueError("invalid co-sim frame length")
            if len(self.buffer) < 4 + length:
                break
            body = bytes(self.buffer[4:4 + length])
            del self.buffer[:4 + length]
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
        self.pending_frames: deque[step_protocol.StepFrame] = deque()

    def receive(self) -> list[step_protocol.StepFrame]:
        frames: list[step_protocol.StepFrame] = list(self.pending_frames)
        self.pending_frames.clear()
        while True:
            try:
                data = self.sock.recv(4096)
            except BlockingIOError:
                break
            if not data:
                raise ConnectionError("QEMU co-sim socket closed")
            self.buffer.extend(data)
        while len(self.buffer) >= 4:
            body_len = struct.unpack_from("<I", self.buffer)[0]
            if body_len < HEADER or body_len > 4 + step_protocol.STEP_HEADER_SIZE + \
                    step_protocol.STEP_MAX_PAYLOAD:
                raise ValueError("invalid co-sim v2 frame length")
            if len(self.buffer) < 4 + body_len:
                break
            raw = bytes(self.buffer[:4 + body_len])
            del self.buffer[:4 + body_len]
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

class CanStream:
    """Fixed-width FDCAN host stream with a bounded non-blocking TX queue."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = bytearray()
        self.tx_queue: deque[memoryview] = deque()

    def receive(self) -> list[tuple[int, int, int, int, bytes]]:
        frames = []
        while True:
            try:
                data = self.sock.recv(4096)
            except BlockingIOError:
                break
            if not data:
                raise ConnectionError("QEMU FDCAN socket closed")
            self.buffer.extend(data)
        while len(self.buffer) >= CAN_WIRE_SIZE:
            body = bytes(self.buffer[:CAN_WIRE_SIZE])
            del self.buffer[:CAN_WIRE_SIZE]
            frame = unpack_wire_frame(body)
            frames.append((frame.can_id, frame.flags, frame.dlc,
                           frame.timestamp_ns, frame.data))
        return frames

    def send(self, can_id: int, flags: int, dlc: int, timestamp_ns: int,
             data: bytes) -> None:
        """Send one fixed-width frame to the QEMU FDCAN chardev."""
        if len(self.tx_queue) >= MAX_PENDING_FRAMES:
            self.flush()
        if len(self.tx_queue) >= MAX_PENDING_FRAMES:
            raise BufferError("FDCAN TX queue is full")
        payload = pack_wire_frame(CanFrame(
            can_id, flags, dlc, data, timestamp_ns))
        self.tx_queue.append(memoryview(payload))
        self.flush()

    @property
    def tx_pending(self) -> bool:
        return bool(self.tx_queue)

    def flush(self) -> bool:
        while self.tx_queue:
            view = self.tx_queue[0]
            try:
                written = self.sock.send(view)
            except BlockingIOError:
                return False
            if written == 0:
                raise ConnectionError("QEMU FDCAN socket closed")
            if written < len(view):
                self.tx_queue[0] = view[written:]
                return False
            self.tx_queue.popleft()
        return True

    def drain(self, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.flush():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("FDCAN TX queue did not drain")
            _, writable, _ = select.select([], [self.sock], [], remaining)
            if not writable:
                raise TimeoutError("QEMU FDCAN socket is not writable")


def send_socketcan(sock: socket.socket, frame: CanFrame) -> None:
    """Send one native CAN/CAN-FD datagram without waiting.

    The worker uses :class:`DatagramQueue` for normal operation.  This small
    helper remains useful to callers that want one-shot forwarding and makes
    backpressure explicit instead of sleeping inside the simulation loop.
    """
    payload = pack_can_frame(frame)
    try:
        written = sock.send(payload)
    except BlockingIOError as exc:
        raise BufferError("SocketCAN TX is backpressured") from exc
    if written != len(payload):
        raise ConnectionError("short SocketCAN datagram write")


class DatagramQueue:
    """Bounded non-blocking queue for datagram-style host endpoints."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.tx_queue: deque[bytes] = deque()

    def send(self, frame: CanFrame) -> None:
        if len(self.tx_queue) >= MAX_PENDING_FRAMES:
            self.flush()
        if len(self.tx_queue) >= MAX_PENDING_FRAMES:
            raise BufferError("SocketCAN TX queue is full")
        self.tx_queue.append(pack_can_frame(frame))
        self.flush()

    @property
    def tx_pending(self) -> bool:
        return bool(self.tx_queue)

    def flush(self) -> bool:
        while self.tx_queue:
            try:
                written = self.sock.send(self.tx_queue[0])
            except BlockingIOError:
                return False
            if written != len(self.tx_queue[0]):
                raise ConnectionError("short SocketCAN datagram write")
            self.tx_queue.popleft()
        return True

    def drain(self, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.flush():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SocketCAN TX queue did not drain")
            _, writable, _ = select.select([], [self.sock], [], remaining)
            if not writable:
                raise TimeoutError("SocketCAN interface is not writable")


def connect_unix(path: str, timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
            time.sleep(0.01)
    raise TimeoutError(f"cannot connect to {path}: {last_error}")


@dataclass
class MotorState:
    values: list[float]
    position: list[float] = field(init=False)
    velocity: list[float] = field(init=False)
    torque: list[float] = field(init=False)
    enabled: list[bool] = field(init=False)
    dm_command: list[dict | None] = field(init=False)

    def __post_init__(self) -> None:
        count = len(self.values)
        self.position = [0.0] * count
        self.velocity = [0.0] * count
        self.torque = [0.0] * count
        self.enabled = [False] * count
        self.dm_command = [None] * count


def _decode_linear(raw: int, bits: int, minimum: float,
                   maximum: float) -> float:
    return raw * (maximum - minimum) / ((1 << bits) - 1) + minimum


def decode_dm_mit(data: bytes, p_max: float = DM_DEFAULT_P_MAX,
                  v_max: float = DM_DEFAULT_V_MAX,
                  t_max: float = DM_DEFAULT_T_MAX) -> dict | None:
    """Decode one DM MIT control packet into physical units."""
    if len(data) != 8:
        return None
    p_raw = (data[0] << 8) | data[1]
    v_raw = (data[2] << 4) | (data[3] >> 4)
    kp_raw = ((data[3] & 0x0f) << 8) | data[4]
    kd_raw = (data[5] << 4) | (data[6] >> 4)
    t_raw = ((data[6] & 0x0f) << 8) | data[7]
    return {
        "position": _decode_linear(p_raw, 16, -p_max, p_max),
        "speed": _decode_linear(v_raw, 12, -v_max, v_max),
        "kp": _decode_linear(kp_raw, 12, 0.0, 500.0),
        "kd": _decode_linear(kd_raw, 12, 0.0, 5.0),
        "torque": _decode_linear(t_raw, 12, -t_max, t_max),
    }


def dm_motor_ids(args: argparse.Namespace, index: int) -> tuple[int, int]:
    """Return the control/slave and feedback IDs for one logical motor."""
    mapping = getattr(args, "dm_motor_map", {})
    if index in mapping:
        return mapping[index]
    return (args.dm_slave_id + index, args.dm_feedback_id + index)


def dm_motor_index(args: argparse.Namespace, can_id: int) -> int | None:
    """Resolve a control ID, preferring explicit per-motor mappings."""
    mapping = getattr(args, "dm_motor_map", {})
    for index, (slave_id, _) in mapping.items():
        if can_id == slave_id:
            return index
    index = can_id - args.dm_slave_id
    return index if 0 <= index < args.motor_count else None


def _encode_linear(value: float, bits: int, minimum: float,
                   maximum: float) -> int:
    value = min(max(value, minimum), maximum)
    return int(round((value - minimum) * ((1 << bits) - 1) /
                     (maximum - minimum)))


def encode_dm_feedback(position: float, velocity: float, torque: float,
                       motor_id: int = DM_DEFAULT_SLAVE_ID,
                       p_max: float = DM_DEFAULT_P_MAX,
                       v_max: float = DM_DEFAULT_V_MAX,
                       t_max: float = DM_DEFAULT_T_MAX) -> bytes:
    """Encode a DM MIT status packet matching motor::dm::decoder."""
    p_raw = _encode_linear(position, 16, -p_max, p_max)
    v_raw = _encode_linear(velocity, 12, -v_max, v_max)
    t_raw = _encode_linear(torque, 12, -t_max, t_max)
    return bytes((
        (motor_id & 0x0f),
        p_raw >> 8, p_raw & 0xff,
        v_raw >> 4,
        ((v_raw & 0x0f) << 4) | (t_raw >> 8),
        t_raw & 0xff,
        25, 25,
    ))


def dm_mit_effort(command: dict, position: float, velocity: float,
                  torque_limit: float) -> float:
    """Evaluate the common MIT command law and apply the plant torque limit."""
    effort = (command["torque"] +
              command["kp"] * (command["position"] - position) +
              command["kd"] * (command["speed"] - velocity))
    return min(max(effort, -torque_limit), torque_limit)


def legacy_motor_effort(value: float, enabled: bool,
                        torque_limit: float) -> float:
    """Return the effective output for the legacy direct-effort command."""
    if not enabled:
        return 0.0
    return min(max(value, -torque_limit), torque_limit)


class NullEngine:
    def __init__(self, motor_count: int, torque_limit: float = DM_DEFAULT_T_MAX):
        self.motors = MotorState([0.0] * motor_count)
        # A deliberately small rigid-rotor approximation.  These defaults
        # make the DM MIT example useful without pretending to be a calibrated
        # actuator model; a real plant can be supplied by MuJoCo/Gazebo.
        self.inertia = 0.02
        self.damping = 0.08
        self.torque_limit = torque_limit

    def set_motor(self, index: int, value: float) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.values[index] = value
            self.motors.dm_command[index] = None
            self.motors.torque[index] = legacy_motor_effort(
                value, self.motors.enabled[index], self.torque_limit)

    def reset(self) -> None:
        """Reset plant state while retaining the engine configuration."""
        count = len(self.motors.values)
        self.motors.values = [0.0] * count
        self.motors.position = [0.0] * count
        self.motors.velocity = [0.0] * count
        self.motors.torque = [0.0] * count
        self.motors.enabled = [False] * count
        self.motors.dm_command = [None] * count

    def set_motor_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.enabled[index] = enabled
            if self.motors.dm_command[index] is None:
                self.motors.torque[index] = legacy_motor_effort(
                    self.motors.values[index], enabled, self.torque_limit)

    def set_motor_dm(self, index: int, command: dict) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.dm_command[index] = command

    def reset_motor(self, index: int) -> None:
        if 0 <= index < len(self.motors.values):
            self.motors.enabled[index] = False
            self.motors.dm_command[index] = None
            self.motors.values[index] = 0.0
            self.motors.position[index] = 0.0
            self.motors.velocity[index] = 0.0
            self.motors.torque[index] = 0.0

    def motor_feedback(self, index: int) -> tuple[float, float, float]:
        if 0 <= index < len(self.motors.values):
            return (self.motors.position[index], self.motors.velocity[index],
                    self.motors.torque[index])
        return (0.0, 0.0, 0.0)

    def step(self, dt: float) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float]]:
        for index, command in enumerate(self.motors.dm_command):
            if command is None:
                applied = legacy_motor_effort(
                    self.motors.values[index], self.motors.enabled[index],
                    self.torque_limit)
            elif self.motors.enabled[index]:
                applied = dm_mit_effort(command, self.motors.position[index],
                                        self.motors.velocity[index],
                                        self.torque_limit)
            else:
                applied = 0.0
            acceleration = (applied - self.damping * self.motors.velocity[index]) / self.inertia
            self.motors.velocity[index] += acceleration * dt
            self.motors.position[index] += self.motors.velocity[index] * dt
            self.motors.torque[index] = applied
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)


class MujocoEngine:
    """Optional MuJoCo adapter with conservative sensor-name conventions."""

    def __init__(self, model_path: str, motor_count: int, timestep: float,
                 torque_limit: float = DM_DEFAULT_T_MAX):
        try:
            import mujoco
        except ImportError as exc:  # pragma: no cover - depends on host setup
            raise RuntimeError(
                "MuJoCo engine requires `uv pip install mujoco`") from exc
        self.mujoco = mujoco
        self.model = mujoco.MjModel.from_xml_path(str(Path(model_path)))
        self.model.opt.timestep = timestep
        self.data = mujoco.MjData(self.model)
        self.motor_count = min(motor_count, self.model.nu)
        self.motor_values = [0.0] * self.motor_count
        self.legacy_values = [0.0] * self.motor_count
        self.torque_limit = torque_limit
        self.enabled = [False] * self.motor_count
        self.dm_commands: list[dict | None] = [None] * self.motor_count
        self.gyro_sensor = self._find_sensor(("imu_gyro", "gyro"))
        self.accel_sensor = self._find_sensor(("imu_accel", "accelerometer", "accel"))

    def _find_sensor(self, names: tuple[str, ...]) -> int | None:
        for name in names:
            sensor_id = self.mujoco.mj_name2id(
                self.model, self.mujoco.mjtObj.mjOBJ_SENSOR, name)
            if sensor_id >= 0:
                return sensor_id
        return None

    def set_motor(self, index: int, value: float) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = value
            self.motor_values[index] = legacy_motor_effort(
                value, self.enabled[index], self.torque_limit)
            self.data.ctrl[index] = self.motor_values[index]

    def reset(self) -> None:
        """Reset MuJoCo state without reloading the model or its sensors."""
        self.data = self.mujoco.MjData(self.model)
        self.motor_values = [0.0] * self.motor_count
        self.legacy_values = [0.0] * self.motor_count
        self.enabled = [False] * self.motor_count
        self.dm_commands = [None] * self.motor_count

    def set_motor_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < self.motor_count:
            self.enabled[index] = enabled
            if self.dm_commands[index] is None:
                self.motor_values[index] = legacy_motor_effort(
                    self.legacy_values[index], enabled, self.torque_limit)
                self.data.ctrl[index] = self.motor_values[index]
            elif not enabled:
                self.motor_values[index] = 0.0
                self.data.ctrl[index] = 0.0

    def set_motor_dm(self, index: int, command: dict) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = command
            if not self.enabled[index]:
                self.motor_values[index] = 0.0
                self.data.ctrl[index] = 0.0

    def reset_motor(self, index: int) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = 0.0
            self.set_motor_enabled(index, False)

    def _joint_state(self, index: int) -> tuple[float, float]:
        if not 0 <= index < self.motor_count:
            return (0.0, 0.0)
        joint = int(self.model.actuator_trnid[index, 0])
        if not 0 <= joint < self.model.njnt:
            return (0.0, 0.0)
        dof = int(self.model.jnt_dofadr[joint])
        qpos = int(self.model.jnt_qposadr[joint])
        return (float(self.data.qpos[qpos]), float(self.data.qvel[dof]))

    def motor_feedback(self, index: int) -> tuple[float, float, float]:
        if not 0 <= index < self.motor_count:
            return (0.0, 0.0, 0.0)
        position, velocity = self._joint_state(index)
        return (position, velocity, self.motor_values[index])

    def _sensor(self, sensor_id: int | None) -> tuple[float, float, float]:
        if sensor_id is None:
            return (0.0, 0.0, 0.0)
        adr = self.model.sensor_adr[sensor_id]
        dim = self.model.sensor_dim[sensor_id]
        values = self.data.sensordata[adr:adr + dim]
        return tuple(float(values[i]) if i < len(values) else 0.0
                     for i in range(3))

    def step(self, dt: float) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float]]:
        if not math.isfinite(dt) or dt <= 0.0:
            raise ValueError("MuJoCo step dt must be finite and positive")
        # The worker's virtual clock is the plant clock.  Update MuJoCo's
        # integration interval for each boundary so variable-rate callers do
        # not silently accumulate time at the startup rate.
        self.model.opt.timestep = dt
        for index, command in enumerate(self.dm_commands):
            if command is None:
                applied = legacy_motor_effort(
                    self.legacy_values[index], self.enabled[index],
                    self.torque_limit)
            else:
                if not self.enabled[index]:
                    applied = 0.0
                else:
                    position, velocity = self._joint_state(index)
                    applied = dm_mit_effort(command, position, velocity,
                                            self.torque_limit)
            self.motor_values[index] = applied
            self.data.ctrl[index] = applied
        self.mujoco.mj_step(self.model, self.data)
        gyro_rad = self._sensor(self.gyro_sensor)
        accel_ms2 = self._sensor(self.accel_sensor)
        return (tuple(v * 180.0 / math.pi for v in gyro_rad),
                tuple(v / 9.80665 for v in accel_ms2))


class Ros2Engine:
    """ROS 2 topic bridge suitable for a Gazebo ROS sensor/controller plugin."""

    def __init__(self, motor_count: int, motor_topic: str, imu_topic: str,
                 joint_state_topic: str, node_name: str,
                 torque_limit: float = DM_DEFAULT_T_MAX,
                 wait_for_imu: bool = False, reset_service: str = "",
                 reset_timeout: float = ROS_RESET_TIMEOUT_SEC):
        try:
            import rclpy
            from rclpy.node import Node
            from sensor_msgs.msg import Imu
            from sensor_msgs.msg import JointState
            from std_msgs.msg import Float64MultiArray
        except ImportError as exc:  # pragma: no cover - depends on ROS setup
            raise RuntimeError(
                "ROS2 engine requires a sourced ROS2 environment") from exc
        self.rclpy = rclpy
        self.motor_count = motor_count
        self.motor_values = [0.0] * motor_count
        self.legacy_values = [0.0] * motor_count
        self.enabled = [False] * motor_count
        self.dm_commands: list[dict | None] = [None] * motor_count
        self.torque_limit = torque_limit
        self.position = [0.0] * motor_count
        self.velocity = [0.0] * motor_count
        self.effort = [0.0] * motor_count
        self.effort_valid = [False] * motor_count
        self.gyro = (0.0, 0.0, 0.0)
        self.accel = (0.0, 0.0, 1.0)
        self.imu_timestamp_ns: int | None = None
        self.wait_for_imu = wait_for_imu
        self.reset_timeout = reset_timeout
        self.reset_client = None
        self.reset_service_type = None
        if not rclpy.ok():
            rclpy.init(args=None)
        self.node = Node(node_name)
        self.message_type = Float64MultiArray
        self.publisher = self.node.create_publisher(Float64MultiArray,
                                                     motor_topic, 10)
        self.subscription = self.node.create_subscription(
            Imu, imu_topic, self._imu_callback, 10)
        self.joint_subscription = self.node.create_subscription(
            JointState, joint_state_topic, self._joint_state_callback, 10)
        if reset_service:
            from std_srvs.srv import Empty
            self.reset_service_type = Empty
            self.reset_client = self.node.create_client(Empty, reset_service)

    def _imu_callback(self, message) -> None:
        self.gyro = tuple(float(value) for value in (
            message.angular_velocity.x,
            message.angular_velocity.y,
            message.angular_velocity.z))
        self.accel = tuple(float(value) / 9.80665 for value in (
            message.linear_acceleration.x,
            message.linear_acceleration.y,
            message.linear_acceleration.z))
        stamp = getattr(getattr(message, "header", None), "stamp", None)
        if stamp is not None:
            seconds = int(getattr(stamp, "sec", -1))
            nanoseconds = int(getattr(stamp, "nanosec", -1))
            if seconds >= 0 and 0 <= nanoseconds < 1_000_000_000:
                self.imu_timestamp_ns = seconds * 1_000_000_000 + nanoseconds

    def _joint_state_callback(self, message) -> None:
        """Read JointState arrays by index; names are an optional plant detail."""
        # JointState permits arrays of different lengths, including omitted
        # fields. Clear values absent from this sample instead of retaining
        # feedback from an older timestamp.
        self.position = [0.0] * self.motor_count
        self.velocity = [0.0] * self.motor_count
        self.effort = [0.0] * self.motor_count
        self.effort_valid = [False] * self.motor_count
        for index in range(min(self.motor_count, len(message.position))):
            self.position[index] = float(message.position[index])
        for index in range(min(self.motor_count, len(message.velocity))):
            self.velocity[index] = float(message.velocity[index])
        for index in range(min(self.motor_count, len(message.effort))):
            self.effort[index] = float(message.effort[index])
            self.effort_valid[index] = True

    def _publish_motors(self) -> None:
        message = self.message_type()
        message.data = list(self.motor_values)
        self.publisher.publish(message)

    def set_motor(self, index: int, value: float) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = value
            self.motor_values[index] = legacy_motor_effort(
                value, self.enabled[index], self.torque_limit)
            self._publish_motors()

    def reset(self) -> None:
        """Reset commands and cached sensor state while keeping ROS alive."""
        self.motor_values = [0.0] * self.motor_count
        self.legacy_values = [0.0] * self.motor_count
        self.enabled = [False] * self.motor_count
        self.dm_commands = [None] * self.motor_count
        self.position = [0.0] * self.motor_count
        self.velocity = [0.0] * self.motor_count
        self.effort = [0.0] * self.motor_count
        self.effort_valid = [False] * self.motor_count
        self.gyro = (0.0, 0.0, 0.0)
        self.accel = (0.0, 0.0, 1.0)
        self.imu_timestamp_ns = None
        self._publish_motors()
        if self.reset_client is not None:
            deadline = time.monotonic() + self.reset_timeout
            future = self.reset_client.call_async(
                self.reset_service_type.Request())
            while self.rclpy.ok() and not future.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ROS2 reset service timed out")
                self.rclpy.spin_once(self.node, timeout_sec=min(0.01, remaining))
            if not future.done():
                raise RuntimeError("ROS2 reset service stopped before completion")
            try:
                future.result()
            except Exception as exc:
                raise RuntimeError("ROS2 reset service failed") from exc

    def set_motor_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < self.motor_count:
            self.enabled[index] = enabled
            if self.dm_commands[index] is None:
                self.motor_values[index] = legacy_motor_effort(
                    self.legacy_values[index], enabled, self.torque_limit)
                self._publish_motors()
            elif not enabled:
                # Keep the existing ROS2 DM-MIT disable behavior: disabling
                # a DM-MIT command clears the active command and publishes 0.
                self.set_motor(index, 0.0)

    def set_motor_dm(self, index: int, command: dict) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = command
            effort = (dm_mit_effort(command, self.position[index],
                                     self.velocity[index], self.torque_limit)
                      if self.enabled[index] else 0.0)
            self.motor_values[index] = effort
            self._publish_motors()

    def reset_motor(self, index: int) -> None:
        if 0 <= index < self.motor_count:
            self.dm_commands[index] = None
            self.legacy_values[index] = 0.0
            self.set_motor_enabled(index, False)

    def motor_feedback(self, index: int) -> tuple[float, float, float]:
        if not 0 <= index < self.motor_count:
            return (0.0, 0.0, 0.0)
        return (self.position[index], self.velocity[index],
                self.effort[index] if self.effort_valid[index] else
                self.motor_values[index])

    def step(self, dt: float) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float]]:
        previous_timestamp = self.imu_timestamp_ns
        if self.wait_for_imu:
            deadline = time.monotonic() + max(0.001, min(dt * 2.0, 0.1))
            while self.rclpy.ok() and time.monotonic() < deadline:
                current_timestamp = self.imu_timestamp_ns
                if (current_timestamp is not None and
                        (previous_timestamp is None or
                         current_timestamp > previous_timestamp)):
                    break
                self.rclpy.spin_once(
                    self.node,
                    timeout_sec=min(0.001, max(0.0, deadline - time.monotonic())))
        else:
            self.rclpy.spin_once(self.node, timeout_sec=0.0)
        return (tuple(value * 180.0 / math.pi for value in self.gyro),
                self.accel)

    def close(self) -> None:
        self.node.destroy_node()
        if self.rclpy.ok():
            self.rclpy.shutdown()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cosim", required=True,
                        help="QEMU co-sim Unix socket")
    parser.add_argument("--protocol", choices=("v1", "v2"), default="v1",
                        help="co-sim control protocol (default: v1)")
    parser.add_argument("--wait-step-done", action="store_true",
                        help="in v2 mode, wait for guest SPI consumption")
    parser.add_argument("--fdcan", help="optional QEMU FDCAN Unix socket")
    parser.add_argument("--socketcan", metavar="IFACE",
                        help="optional Linux SocketCAN interface (for --fdcan)")
    parser.add_argument("--engine", default="null",
                        help="registered backend name (default: null)")
    parser.add_argument("--backend", metavar="MODULE:FACTORY",
                        help="load one backend factory and use it directly")
    parser.add_argument("--backend-registry", metavar="MODULE:INITIALIZER",
                        help="load a registry initializer before selecting --engine")
    parser.add_argument("--mujoco-model", help="MuJoCo XML/MJB model path")
    parser.add_argument("--motor-protocol", choices=("auto", "float", "dm-mit"),
                        default="auto",
                        help="CAN motor command protocol (default: auto)")
    parser.add_argument("--dm-slave-id", type=lambda value: int(value, 0),
                        default=DM_DEFAULT_SLAVE_ID)
    parser.add_argument("--dm-feedback-id", type=lambda value: int(value, 0),
                        default=DM_DEFAULT_FEEDBACK_ID)
    parser.add_argument("--dm-motor", action="append", default=[],
                        metavar="INDEX:SLAVE_ID:FEEDBACK_ID",
                        help="override DM IDs for one motor (repeatable)")
    parser.add_argument("--dm-p-max", type=float, default=DM_DEFAULT_P_MAX)
    parser.add_argument("--dm-v-max", type=float, default=DM_DEFAULT_V_MAX)
    parser.add_argument("--dm-t-max", type=float, default=DM_DEFAULT_T_MAX)
    parser.add_argument("--ros-motor-topic", default="/dm_mc02/motor_cmd")
    parser.add_argument("--ros-imu-topic", default="/dm_mc02/imu")
    parser.add_argument("--ros-joint-state-topic",
                        default="/dm_mc02/joint_states",
                        help="ROS2 sensor_msgs/JointState topic for motor state")
    parser.add_argument("--ros-node-name", default="dm_mc02_sim_worker")
    parser.add_argument("--ros-wait-imu", action="store_true",
                        help="wait for a newer ROS IMU timestamp at each step")
    parser.add_argument("--ros-reset-service", default="",
                        help="optional ROS2 Empty service used by reset")
    parser.add_argument("--ros-reset-timeout", type=float,
                        default=ROS_RESET_TIMEOUT_SEC,
                        help="ROS2 reset service timeout in seconds")
    parser.add_argument("--motor-count", type=int, default=4)
    parser.add_argument("--motor-can-base", type=lambda x: int(x, 0),
                        default=0x200)
    parser.add_argument("--rate", type=float, default=1000.0)
    parser.add_argument("--adc-input", action="append", default=[],
                        metavar="CHANNEL:RAW",
                        help="inject an ADC input on every simulation step")
    parser.add_argument("--adc-voltage", action="append", default=[],
                        metavar="CHANNEL:VOLTS",
                        help="inject an ADC analog voltage on every simulation step")
    parser.add_argument("--realtime", action="store_true",
                        help="pace virtual time against wall clock")
    parser.add_argument("--frames", type=int, default=0,
                        help="stop after this many IMU frames (0 = forever)")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.rate <= 0 or args.motor_count < 0:
        parser.error("rate must be positive and motor-count non-negative")
    if args.socketcan and not args.fdcan:
        parser.error("--socketcan requires --fdcan")
    if args.backend and args.backend_registry:
        parser.error("--backend and --backend-registry cannot be combined")
    if not args.engine.strip():
        parser.error("engine name must be non-empty")
    if args.wait_step_done and args.protocol != "v2":
        parser.error("--wait-step-done requires --protocol v2")
    if not 0 <= args.dm_slave_id <= 0x7ff or not 0 <= args.dm_feedback_id <= 0x7ff:
        parser.error("DM CAN IDs must be standard 11-bit identifiers")
    args.dm_motor_map = {}
    for spec in args.dm_motor:
        try:
            index_text, slave_text, feedback_text = spec.split(":", 2)
            index = int(index_text, 0)
            slave_id = int(slave_text, 0)
            feedback_id = int(feedback_text, 0)
        except ValueError:
            parser.error("--dm-motor must use INDEX:SLAVE_ID:FEEDBACK_ID")
        if not 0 <= index < args.motor_count:
            parser.error("--dm-motor index must be within motor-count")
        if not 0 <= slave_id <= 0x7ff or not 0 <= feedback_id <= 0x7ff:
            parser.error("--dm-motor CAN IDs must be standard 11-bit identifiers")
        if index in args.dm_motor_map:
            parser.error("--dm-motor index must not be repeated")
        args.dm_motor_map[index] = (slave_id, feedback_id)
    # Reject collisions in the complete effective map, including contiguous
    # defaults for indexes that were not explicitly overridden. A feedback ID
    # colliding with another control ID is unsafe because feedback could be
    # interpreted as a new command by this worker.
    seen_ids = {}
    for index in range(args.motor_count):
        slave_id, feedback_id = dm_motor_ids(args, index)
        for role, can_id in (("slave", slave_id), ("feedback", feedback_id)):
            if not 0 <= can_id <= 0x7ff:
                parser.error(
                    "effective DM CAN ID for motor %d %s must be "
                    "a standard 11-bit identifier" % (index, role))
            previous = seen_ids.get(can_id)
            if previous is not None:
                parser.error(
                    "effective DM CAN ID 0x%x is used by motor %d %s and "
                    "motor %d %s" % (can_id, previous[0], previous[1],
                                     index, role))
            seen_ids[can_id] = (index, role)
    if (not math.isfinite(args.dm_p_max) or not math.isfinite(args.dm_v_max) or
            not math.isfinite(args.dm_t_max) or args.dm_p_max <= 0 or
            args.dm_v_max <= 0 or args.dm_t_max <= 0):
        parser.error("DM motor limits must be finite and positive")
    if args.engine == "mujoco" and not args.mujoco_model:
        parser.error("--mujoco-model is required with --engine mujoco")
    if (not math.isfinite(args.ros_reset_timeout) or
            args.ros_reset_timeout <= 0):
        parser.error("ros-reset-timeout must be finite and positive")
    args.adc_input_values = []
    for spec in args.adc_input:
        try:
            channel_text, raw_text = spec.split(":", 1)
            channel, raw = int(channel_text, 0), int(raw_text, 0)
        except ValueError:
            parser.error("--adc-input must use CHANNEL:RAW")
        if not 0 <= channel <= 31 or not 0 <= raw <= 0xffff:
            parser.error("--adc-input channel must be 0..31 and raw 0..65535")
        args.adc_input_values.append((channel, raw))
    args.adc_voltage_values = []
    for spec in args.adc_voltage:
        try:
            channel_text, volts_text = spec.split(":", 1)
            channel = int(channel_text, 0)
            volts = float(volts_text)
        except ValueError:
            parser.error("--adc-voltage must use CHANNEL:VOLTS")
        if not 0 <= channel <= 31 or not math.isfinite(volts):
            parser.error("--adc-voltage channel must be 0..31 and volts finite")
        voltage_uv = int(round(volts * 1_000_000.0))
        if not 0 <= voltage_uv <= ADC_VOLTAGE_MAX_UV:
            parser.error("--adc-voltage must be in range 0..3.3 V")
        args.adc_voltage_values.append((channel, voltage_uv))
    return args


def decode_motor_command(can_id: int, flags: int, data: bytes,
                         args: argparse.Namespace) -> tuple | None:
    """Return ``(kind, index, value)`` for a supported motor CAN packet."""
    if flags & (CAN_FLAG_RTR | CAN_FLAG_FD):
        return None

    if args.motor_protocol in ("auto", "dm-mit"):
        index = dm_motor_index(args, can_id)
        if index is not None and len(data) == 8:
            if data == DM_RESET_COMMAND:
                return ("dm-reset", index, None)
            if data == DM_ENABLE_COMMAND:
                return ("dm-enable", index, None)
            if data == DM_DISABLE_COMMAND:
                return ("dm-disable", index, None)
            command = decode_dm_mit(data, args.dm_p_max, args.dm_v_max,
                                    args.dm_t_max)
            if command is not None:
                return ("dm-control", index, command)

    if args.motor_protocol in ("auto", "float"):
        index = can_id - args.motor_can_base
        if 0 <= index < args.motor_count and len(data) >= 4:
            value = struct.unpack("<f", data[:4])[0]
            if math.isfinite(value):
                return ("float", index, value)
    return None


def apply_motor_command(engine, command: tuple) -> None:
    kind, index, value = command
    if kind == "float":
        engine.set_motor(index, value)
    elif kind == "dm-reset":
        engine.reset_motor(index)
    elif kind == "dm-enable":
        engine.set_motor_enabled(index, True)
    elif kind == "dm-disable":
        engine.set_motor_enabled(index, False)
    elif kind == "dm-control":
        engine.set_motor_dm(index, value)


def send_dm_feedback(can: CanStream, engine, indices: Iterable[int],
                     args: argparse.Namespace, timestamp_ns: int,
                     adapter: DmMotorBusAdapter | None = None) -> None:
    for index in indices:
        if adapter is not None:
            state = adapter.state(engine, index, timestamp_ns)
            feedback_id, payload = adapter.encode_feedback(state)
        else:
            position, velocity, torque = engine.motor_feedback(index)
            slave_id, feedback_id = dm_motor_ids(args, index)
            payload = encode_dm_feedback(position, velocity, torque,
                                         slave_id, args.dm_p_max,
                                         args.dm_v_max, args.dm_t_max)
        can.send(feedback_id, 0, 8, timestamp_ns,
                 payload)


def create_backend_registry() -> BackendRegistry:
    """Create the built-in backend table without sharing global state."""
    registry = BackendRegistry()
    registry.register(
        "null", lambda config: NullEngine(config.motor_count,
                                           config.dm_t_max))
    registry.register(
        "mujoco", lambda config: MujocoEngine(
            config.mujoco_model, config.motor_count, 1.0 / config.rate,
            config.dm_t_max))
    registry.register(
        "ros2", lambda config: Ros2Engine(
            config.motor_count, config.ros_motor_topic, config.ros_imu_topic,
            config.ros_joint_state_topic, config.ros_node_name,
            config.dm_t_max, config.ros_wait_imu, config.ros_reset_service,
            config.ros_reset_timeout))
    return registry


def create_engine(args: argparse.Namespace):
    """Construct a backend from a direct factory or an isolated registry."""
    if args.backend:
        return load_factory(args.backend)(args)
    registry = create_backend_registry()
    if args.backend_registry:
        load_registry(registry, args.backend_registry)
    return registry.create(args.engine, args)


def engine_uses_external_time(engine) -> bool:
    """Backends opt into external timestamp mapping by exposing this field."""
    return hasattr(engine, "imu_timestamp_ns")


def run(args: argparse.Namespace) -> int:
    cosim_sock = connect_unix(args.cosim, args.connect_timeout)
    cosim = (StepFramedSocket(cosim_sock) if args.protocol == "v2"
             else FramedSocket(cosim_sock))
    can_sock = connect_unix(args.fdcan, args.connect_timeout) if args.fdcan else None
    can = CanStream(can_sock) if can_sock else None
    socketcan_sock = open_socketcan(args.socketcan) if args.socketcan else None
    socketcan_tx = DatagramQueue(socketcan_sock) if socketcan_sock else None
    engine = create_engine(args)
    motor_adapter = DmMotorBusAdapter.from_namespace(args)
    coordinator = StepCoordinator(engine, motor_adapter)

    cosim_sock.setblocking(False)
    # The initial telemetry timestamp is the best available common clock
    # origin for v1. v2 deliberately starts the plant clock at zero and
    # reports QEMU's current clock in RESET_ACK payload metadata.
    clock_base_ns = 0
    v2_tx_validator = None
    v2_rx_validator = None
    qemu_timestamp_mapper = None
    if args.protocol == "v1":
        # QEMU emits its initial snapshot synchronously on chardev open. Keep
        # a short grace window for scheduling, but do not impose a large
        # startup stall on endpoints that intentionally omit telemetry.
        if select.select([cosim_sock], [], [], 0.02)[0]:
            for kind, _, timestamp_ns, payload in cosim.receive():
                if kind == TELEMETRY and len(payload) == 12:
                    clock_base_ns = max(clock_base_ns, timestamp_ns)
        # Establish the QEMU RX ordering baseline before the first IMU sample.
        cosim.send(protocol_frame(RESET, 0, clock_base_ns))
    else:
        # A non-zero session ID prevents delayed frames from an earlier
        # machine reset from being accepted by the new QEMU epoch.  The
        # legacy zero-ID mode remains available to direct callers and old
        # smoke clients.
        session_id = step_protocol.generate_session_id()
        v2_tx_validator = step_protocol.StepSessionValidator(
            step_protocol.DIRECTION_HOST_TO_QEMU)
        v2_rx_validator = step_protocol.StepSessionValidator(
            step_protocol.DIRECTION_QEMU_TO_HOST,
            require_step_done=args.wait_step_done)
        reset_frame = step_protocol.StepFrame(
            step_protocol.StepHeader(step_protocol.STEP_VERSION,
                                     step_protocol.RESET, 0, 0, 0, 0,
                                     session_id),
            b"")
        v2_tx_validator.accept(reset_frame)
        v2_rx_validator.begin_reset(reset_frame.header.step_id,
                                    reset_frame.header.t_sim_ns,
                                    session_id)
        cosim.send(step_protocol.encode_step_frame(reset_frame))
        reset_ack_frame = wait_v2_response(
            cosim, v2_rx_validator, step_protocol.RESET_ACK, 0, 0)
        reset_ack = step_protocol.decode_reset_ack_payload(
            reset_ack_frame.payload)
        qemu_timestamp_mapper = QemuTimestampMapper(
            reset_ack.virtual_time_ns, 0)
    if args.protocol == "v1":
        qemu_timestamp_mapper = QemuTimestampMapper(
            clock_base_ns, clock_base_ns)
    dt = 1.0 / args.rate
    integer_rate = (int(args.rate) if args.rate.is_integer() else 0)
    sequence = 0
    imu_frames = 0
    virtual_time_ns = clock_base_ns
    external_timestamp_mapper = (
        ExternalTimestampMapper(clock_base_ns)
        if engine_uses_external_time(engine) else None)
    wall_origin = time.monotonic_ns()

    def reset_v2_worker_session(reset: V2SessionReset) -> None:
        """Align host-side state with QEMU's unsolicited reset epoch."""
        nonlocal clock_base_ns, external_timestamp_mapper, imu_frames
        nonlocal sequence, virtual_time_ns, wall_origin
        nonlocal qemu_timestamp_mapper

        reset_frame = step_protocol.StepFrame(
            step_protocol.StepHeader(
                step_protocol.STEP_VERSION, step_protocol.RESET, 0,
                reset.step_id, reset.t_sim_ns, 0, reset.session_id),
            b"")
        v2_tx_validator.reset()
        v2_tx_validator.accept(reset_frame)
        coordinator.reset()
        if hasattr(engine, "reset"):
            engine.reset()
        clock_base_ns = reset.t_sim_ns
        virtual_time_ns = reset.t_sim_ns
        sequence = 0
        imu_frames = 0
        wall_origin = time.monotonic_ns()
        external_timestamp_mapper = (
            ExternalTimestampMapper(clock_base_ns)
            if engine_uses_external_time(engine) else None)
        qemu_timestamp_mapper = QemuTimestampMapper(
            reset.t_sim_ns, reset.t_sim_ns)

    def wait_v2_step_response(expected_kind: int, step_id: int,
                              timestamp_ns: int,
                              allow_queue_full: bool = False,
                              timeout: float = V2_STEP_RESPONSE_TIMEOUT_SEC):
        try:
            return wait_v2_response(
                cosim, v2_rx_validator, expected_kind, step_id, timestamp_ns,
                timeout=timeout, allow_queue_full=allow_queue_full)
        except V2SessionReset as reset:
            reset_v2_worker_session(reset)
            return False
        return True

    if can_sock:
        can_sock.setblocking(False)
    if socketcan_sock:
        socketcan_sock.setblocking(False)

    try:
        while args.frames == 0 or imu_frames < args.frames:
            readable = [cosim_sock]
            writable = []
            if can_sock:
                readable.append(can_sock)
            if socketcan_sock:
                readable.append(socketcan_sock)
            if cosim.tx_pending:
                writable.append(cosim_sock)
            if can and can.tx_pending:
                writable.append(can_sock)
            if socketcan_tx and socketcan_tx.tx_pending:
                writable.append(socketcan_sock)
            ready, writable_ready, _ = select.select(readable, writable, [], 0)
            if cosim_sock in writable_ready:
                cosim.flush()
            if can_sock and can_sock in writable_ready:
                can.flush()
            if socketcan_sock and socketcan_sock in writable_ready:
                socketcan_tx.flush()
            if can_sock in ready:
                can_frames = can.receive()
                if args.verbose and can_frames:
                    print("received CAN frames", len(can_frames), flush=True)
                for can_id, flags, dlc, timestamp_ns, data in can_frames:
                    mapped_timestamp_ns = qemu_timestamp_mapper.map(
                        timestamp_ns, virtual_time_ns)
                    if socketcan_sock:
                        socketcan_tx.send(
                            CanFrame(can_id, flags, dlc, data,
                                     mapped_timestamp_ns))
                    command = coordinator.submit_can(
                        can_id, flags, data[:min(dlc, 8)],
                        mapped_timestamp_ns, virtual_time_ns)
                    if command is not None:
                        if args.verbose:
                            print("motor command", command.mode,
                                  "index", command.index, flush=True)
            if socketcan_sock in ready:
                try:
                    frame = unpack_can_frame(
                        socketcan_sock.recv(72), timestamp_ns=virtual_time_ns)
                except ValueError as exc:
                    if args.verbose:
                        print(f"socketcan: ignored frame: {exc}",
                              file=sys.stderr, flush=True)
                else:
                    if can:
                        can.send(
                            frame.can_id, frame.flags, frame.dlc,
                            qemu_timestamp_mapper.unmap(
                                frame.timestamp_ns,
                                qemu_timestamp_mapper.qemu_origin_ns),
                            frame.data)
            if cosim_sock in ready:
                if args.protocol == "v1":
                    for kind, _, _, payload in cosim.receive():
                        if kind == TELEMETRY and args.verbose:
                            if len(payload) == 12:
                                rgb, brightness, buzzer, flags, _ = \
                                    struct.unpack("<IIBBH", payload)
                                print("telemetry rgb=%06x brightness=%d buzzer=%d flags=%02x" %
                                      (rgb, brightness, buzzer, flags), flush=True)

            gyro, accel = coordinator.advance(virtual_time_ns, dt)
            adc_count = (len(args.adc_input_values) +
                         len(args.adc_voltage_values)
                         if args.protocol == "v1" else 0)
            # Derive each IMU timestamp from the sample index instead of
            # repeatedly adding a rounded 1/rate interval.  The latter
            # accumulates a deterministic drift at non-integral rates such as
            # 333 Hz.  ADC frames still occupy strictly increasing sub-ticks;
            # at ordinary rates they fit inside the ideal IMU interval.
            if integer_rate:
                # Exact round-half-to-even for the common integer-rate path;
                # this is equivalent to the previous float round() result
                # without a per-step floating-point division.
                numerator = (imu_frames + 1) * 1_000_000_000
                quotient, remainder = divmod(numerator, integer_rate)
                twice_remainder = remainder * 2
                if (twice_remainder > integer_rate or
                        (twice_remainder == integer_rate and quotient & 1)):
                    quotient += 1
                ideal_time_ns = clock_base_ns + quotient
            else:
                ideal_time_ns = clock_base_ns + int(round(
                    (imu_frames + 1) * 1_000_000_000.0 / args.rate))
            if external_timestamp_mapper is not None:
                ideal_time_ns = external_timestamp_mapper.map(
                    getattr(engine, "imu_timestamp_ns", None), ideal_time_ns)
            step_span_ns = max(ideal_time_ns - virtual_time_ns,
                               adc_count + 1)
            virtual_time_ns += step_span_ns
            if args.protocol == "v1":
                for input_index, (channel, raw) in enumerate(
                        args.adc_input_values):
                    sequence += 1
                    cosim.send(adc_input_frame(
                        sequence, virtual_time_ns - step_span_ns +
                        input_index + 1, channel, raw))
                for input_index, (channel, voltage_uv) in enumerate(
                        args.adc_voltage_values,
                        start=len(args.adc_input_values)):
                    sequence += 1
                    cosim.send(adc_voltage_frame(
                        sequence, virtual_time_ns - step_span_ns +
                        input_index + 1, channel, voltage_uv))
                sequence += 1
                cosim.send(imu_frame(sequence, virtual_time_ns, gyro, accel))
            else:
                step_id = imu_frames + 1
                imu_sample = step_protocol.ImuSampleV2(
                    tuple(gyro), tuple(accel), (0.0,) * 3, (0.0,) * 3,
                    virtual_time_ns, step_id)
                if args.adc_input_values or args.adc_voltage_values:
                    imu_payload = step_protocol.build_step_input(
                        imu_sample,
                        (step_protocol.AdcInputV2(channel, raw)
                         for channel, raw in args.adc_input_values),
                        (step_protocol.AdcVoltageV2(channel, voltage_uv)
                         for channel, voltage_uv in args.adc_voltage_values))
                else:
                    # Preserve the compact v2 IMU-only payload for clients
                    # that do not need extensible input sections.
                    imu_payload = step_protocol.encode_imu_sample_v2(imu_sample)
                step_frame = step_protocol.StepFrame(
                    step_protocol.StepHeader(
                        step_protocol.STEP_VERSION, step_protocol.STEP,
                        len(imu_payload), step_id, virtual_time_ns,
                        step_span_ns, v2_tx_validator.session_id),
                    imu_payload)
                v2_tx_validator.accept(step_frame)
                step_wire = step_protocol.encode_step_frame(step_frame)
                cosim.send(step_wire)
                reset_during_step = False
                ack_retries = 0
                while True:
                    try:
                        response = wait_v2_step_response(
                            step_protocol.STEP_ACK, step_id, virtual_time_ns,
                            allow_queue_full=True,
                            timeout=V2_STEP_ACK_TIMEOUT_SEC)
                    except TimeoutError:
                        if ack_retries >= V2_STEP_MAX_RETRIES:
                            raise
                        ack_retries += 1
                        # QEMU treats an identical STEP in the same session as
                        # an idempotent acknowledgement request.
                        cosim.send(step_wire)
                        continue
                    if response is False:
                        reset_during_step = True
                        break
                    response_ack = step_protocol.decode_step_ack_payload(
                        response.payload)
                    if response_ack.status != step_protocol.STATUS_QUEUE_FULL:
                        break
                    # QEMU has retained this exact STEP for a safe retry. Do
                    # not re-run the host validator: it is one logical input.
                    cosim.send(step_wire)
                if reset_during_step:
                    continue
                if args.wait_step_done:
                    done_retries = 0
                    while True:
                        try:
                            done_response = wait_v2_step_response(
                                step_protocol.STEP_DONE, step_id,
                                virtual_time_ns)
                        except TimeoutError:
                            if done_retries >= V2_STEP_DONE_MAX_RETRIES:
                                raise
                            done_retries += 1
                            cosim.send(step_wire)
                            continue
                        break
                    if done_response is False:
                        continue
            if can and coordinator.active_indices:
                send_dm_feedback(can, engine, sorted(coordinator.active_indices), args,
                                  virtual_time_ns, motor_adapter)
            imu_frames += 1
            if args.realtime:
                # wall_origin corresponds to clock_base_ns, not virtual time
                # zero.  Subtract the common origin so a QEMU endpoint that
                # has already advanced its virtual clock does not add an
                # artificial startup delay.
                target = realtime_target_ns(wall_origin, virtual_time_ns,
                                            clock_base_ns)
                remaining = target - time.monotonic_ns()
                if remaining > 0:
                    time.sleep(remaining / 1_000_000_000)
        cosim.drain()
        if can:
            can.drain()
        if socketcan_tx:
            socketcan_tx.drain()
    finally:
        if hasattr(engine, "close"):
            engine.close()
        cosim_sock.close()
        if can_sock:
            can_sock.close()
        if socketcan_sock:
            socketcan_sock.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except (BufferError, ConnectionError, OSError, TimeoutError, ValueError, RuntimeError,
            SocketCANError) as exc:
        print(f"dm_mc02_sim_worker: {exc}", file=sys.stderr)
        raise SystemExit(1)
