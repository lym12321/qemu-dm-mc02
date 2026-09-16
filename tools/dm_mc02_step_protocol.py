"""Version 2 step/ack wire codec for the DM-MC02 plant interface.

The codec intentionally has no transport or worker dependencies.  All wire
integers are little-endian and all payload sizes are exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import secrets
import struct
from typing import Iterable, Sequence


STEP_VERSION = 2
STEP_MAGIC = 0x32434D44
STEP_HEADER_FORMAT = "<HHIQQQI"
STEP_HEADER_SIZE = struct.calcsize(STEP_HEADER_FORMAT)
STEP_OUTER_SIZE = 4
STEP_FRAME_PREFIX_SIZE = STEP_OUTER_SIZE + 4
# Keep the public codec bounded to the payload accepted by the native QEMU
# endpoint. Larger plant messages must use a separate transport or be split
# into multiple sections/steps.
STEP_MAX_PAYLOAD = 512
STEP_SECTION_HEADER_FORMAT = "<HHI"
STEP_SECTION_HEADER_SIZE = struct.calcsize(STEP_SECTION_HEADER_FORMAT)
STEP_PAYLOAD_HEADER_FORMAT = "<HH"
STEP_PAYLOAD_HEADER_SIZE = struct.calcsize(STEP_PAYLOAD_HEADER_FORMAT)
# The old second u16 was reserved and zero. A marker makes section payloads
# unambiguous when their total length happens to be 60 bytes.
STEP_PAYLOAD_SECTION_MARKER = 0x5343  # ASCII "SC"
STEP_PAYLOAD_MAX = STEP_MAX_PAYLOAD

RESET = 1
STEP = 2
STEP_ACK = 3
DIAGNOSTICS = 4
RESET_ACK = 5
STEP_DONE = 6
TELEMETRY = 7
MOTOR_STATE = 8

# v2 response status values. Keep these aligned with the native QEMU link.
V2_STATUS_OK = 0
V2_STATUS_QUEUE_FULL = 1
V2_STATUS_PROTOCOL = 2
V2_STATUS_UNSUPPORTED = 3

# v2 RESET_ACK capability bits.
V2_CAP_STEP = 1 << 0
V2_CAP_IMU = 1 << 1
V2_CAP_DIAGNOSTICS = 1 << 2
V2_CAP_ADC = 1 << 3
V2_CAP_TELEMETRY = 1 << 4
V2_CAP_MOTOR = 1 << 5
V2_CAPABILITY_MASK = (V2_CAP_STEP | V2_CAP_IMU | V2_CAP_DIAGNOSTICS |
                      V2_CAP_ADC | V2_CAP_TELEMETRY | V2_CAP_MOTOR)

# Fixed response payload sizes, excluding the v2 frame header.
RESET_ACK_PAYLOAD_SIZE = 24
STEP_ACK_PAYLOAD_SIZE = 16
DIAGNOSTICS_PAYLOAD_SIZE = 40
STEP_DONE_PAYLOAD_SIZE = 20

# A STEP_DONE describes consumption of the two BMI088 direct-data streams.
# Keep this mask deliberately small: accepting unknown bits would make a
# successful completion ambiguous when the wire protocol grows new sensors.
V2_STEP_DONE_ACCEL = 1 << 0
V2_STEP_DONE_GYRO = 1 << 1
V2_STEP_DONE_REQUIRED_MASK = V2_STEP_DONE_ACCEL | V2_STEP_DONE_GYRO

# Short, descriptive aliases for callers that do not use the V2 prefix.
STATUS_OK = V2_STATUS_OK
STATUS_QUEUE_FULL = V2_STATUS_QUEUE_FULL
STATUS_PROTOCOL = V2_STATUS_PROTOCOL
STATUS_UNSUPPORTED = V2_STATUS_UNSUPPORTED
CAP_STEP = V2_CAP_STEP
CAP_IMU = V2_CAP_IMU
CAP_DIAGNOSTICS = V2_CAP_DIAGNOSTICS
CAP_ADC = V2_CAP_ADC
CAP_TELEMETRY = V2_CAP_TELEMETRY
CAP_MOTOR = V2_CAP_MOTOR
CAPABILITY_MASK = V2_CAPABILITY_MASK
RESET_ACK_V2_PAYLOAD_SIZE = RESET_ACK_PAYLOAD_SIZE
STEP_ACK_V2_PAYLOAD_SIZE = STEP_ACK_PAYLOAD_SIZE
DIAGNOSTICS_V2_PAYLOAD_SIZE = DIAGNOSTICS_PAYLOAD_SIZE
STEP_DONE_V2_PAYLOAD_SIZE = STEP_DONE_PAYLOAD_SIZE
STEP_DONE_ACCEL_MASK = V2_STEP_DONE_ACCEL
STEP_DONE_GYRO_MASK = V2_STEP_DONE_GYRO
STEP_DONE_REQUIRED_MASK = V2_STEP_DONE_REQUIRED_MASK

BOARD_TELEMETRY_PAYLOAD_FORMAT = "<IIIIII"
BOARD_TELEMETRY_PAYLOAD_SIZE = struct.calcsize(
    BOARD_TELEMETRY_PAYLOAD_FORMAT)

STEP_SECTION_IMU_SAMPLE = 1
STEP_SECTION_MOTOR_COMMAND = 2
STEP_SECTION_ADC_INPUT = 3
STEP_SECTION_ADC_VOLTAGE = 4
STEP_SECTION_MOTOR_STATE = 5

UINT16_MAX = (1 << 16) - 1
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1


def _generate_session_id() -> int:
    """Return a non-zero session ID for the v2 header flags slot."""
    # Zero is reserved for the legacy v2 mode.
    return secrets.randbits(32) or 1


generate_session_id = _generate_session_id


@dataclass(frozen=True)
class StepHeader:
    version: int = STEP_VERSION
    kind: int = 0
    payload_len: int = 0
    step_id: int = 0
    t_sim_ns: int = 0
    dt_ns: int = 0
    # Kept under its historical name for positional/API compatibility.  On
    # the v2 wire this u32 is the optional session ID; zero means legacy v2.
    flags: int = 0

    @property
    def session_id(self) -> int:
        """The v2 session ID carried by the legacy-compatible flags field."""
        return self.flags


@dataclass(frozen=True)
class MotorCommandItem:
    index: int
    command: float


@dataclass(frozen=True)
class MotorCommand:
    motor_count: int
    flags: int = 0
    commands: tuple[MotorCommandItem, ...] = ()


@dataclass(frozen=True)
class MotorStateItem:
    index: int
    position: float
    velocity: float
    effort: float


@dataclass(frozen=True)
class MotorState:
    motor_count: int
    states: tuple[MotorStateItem, ...] = ()


@dataclass(frozen=True)
class ImuSampleV2:
    gyro_dps: tuple[float, float, float]
    accel_g: tuple[float, float, float]
    gyro_bias_dps: tuple[float, float, float]
    accel_bias_g: tuple[float, float, float]
    sensor_time_ns: int
    sample_id: int


@dataclass(frozen=True)
class StepSection:
    type: int
    flags: int = 0
    payload: bytes = b""


@dataclass(frozen=True)
class AdcInputV2:
    channel: int
    raw: int


@dataclass(frozen=True)
class AdcVoltageV2:
    channel: int
    voltage_uv: int
    flags: int = 0


@dataclass(frozen=True)
class StepFrame:
    header: StepHeader
    payload: bytes = b""


@dataclass(frozen=True)
class ResetAckPayload:
    status: int
    capabilities: int
    virtual_time_ns: int
    imu_queue_depth: int


@dataclass(frozen=True)
class StepAckPayload:
    status: int
    queue_depth: int
    dropped_count: int


@dataclass(frozen=True)
class DiagnosticsPayload:
    rx_frames: int
    rx_bad_frames: int
    rx_dropped_bytes: int
    tx_frames: int
    tx_dropped: int


@dataclass(frozen=True)
class StepDonePayload:
    status: int
    consumed_mask: int
    missing_mask: int
    queue_depth: int
    dropped_count: int


@dataclass(frozen=True)
class BoardTelemetryPayload:
    led_rgb: int
    led_brightness: int
    buzzer: int
    board_flags: int
    buzzer_frequency_hz: int
    buzzer_duty_permille: int


# Explicit V2 names are useful in codebases that also have older payloads.
ResetAckPayloadV2 = ResetAckPayload
StepAckPayloadV2 = StepAckPayload
DiagnosticsPayloadV2 = DiagnosticsPayload
StepDonePayloadV2 = StepDonePayload


class StepProtocolError(ValueError):
    """Raised for malformed v2 frames or invalid session transitions."""


class StepSessionDirection(str, Enum):
    """The wire direction owned by one session validator."""

    AUTO = "auto"
    HOST_TO_QEMU = "host_to_qemu"
    QEMU_TO_HOST = "qemu_to_host"


DIRECTION_AUTO = StepSessionDirection.AUTO.value
DIRECTION_HOST_TO_QEMU = StepSessionDirection.HOST_TO_QEMU.value
DIRECTION_QEMU_TO_HOST = StepSessionDirection.QEMU_TO_HOST.value
HOST_TO_QEMU = DIRECTION_HOST_TO_QEMU
QEMU_TO_HOST = DIRECTION_QEMU_TO_HOST


def _validate_status(status: int) -> int:
    if status not in (V2_STATUS_OK, V2_STATUS_QUEUE_FULL, V2_STATUS_PROTOCOL,
                      V2_STATUS_UNSUPPORTED):
        raise StepProtocolError(f"unsupported v2 status: {status}")
    return status


def _validate_payload_bytes(data: bytes, expected: int, name: str) -> None:
    if len(data) != expected:
        raise StepProtocolError(
            f"{name} must be exactly {expected} bytes")


def validate_reset_ack_payload(data: bytes) -> None:
    """Validate one native v2 RESET_ACK payload."""
    _validate_payload_bytes(data, RESET_ACK_PAYLOAD_SIZE, "RESET_ACK payload")
    status, capabilities = struct.unpack_from("<II", data)
    _validate_status(status)
    if capabilities & ~V2_CAPABILITY_MASK:
        raise StepProtocolError("RESET_ACK has unsupported capability bits")
    if struct.unpack_from("<I", data, 20)[0] != 0:
        raise StepProtocolError("RESET_ACK reserved field is not zero")


def decode_reset_ack_payload(data: bytes) -> ResetAckPayload:
    """Decode and strictly validate a v2 RESET_ACK payload."""
    validate_reset_ack_payload(data)
    status, capabilities, virtual_time_ns, imu_queue_depth, _ = \
        struct.unpack("<IIQII", data)
    return ResetAckPayload(status, capabilities, virtual_time_ns,
                           imu_queue_depth)


def encode_reset_ack_payload(value: ResetAckPayload) -> bytes:
    """Encode one native v2 RESET_ACK payload."""
    status = _uint(value.status, UINT32_MAX, "RESET_ACK status")
    _validate_status(status)
    capabilities = _uint(value.capabilities, UINT32_MAX,
                          "RESET_ACK capabilities")
    if capabilities & ~V2_CAPABILITY_MASK:
        raise StepProtocolError("RESET_ACK has unsupported capability bits")
    virtual_time_ns = _time(value.virtual_time_ns,
                            "RESET_ACK virtual_time_ns")
    queue_depth = _uint(value.imu_queue_depth, UINT32_MAX,
                        "RESET_ACK queue depth")
    return struct.pack("<IIQII", status, capabilities, virtual_time_ns,
                       queue_depth, 0)


def validate_step_ack_payload(data: bytes) -> None:
    """Validate one native v2 STEP_ACK payload."""
    _validate_payload_bytes(data, STEP_ACK_PAYLOAD_SIZE, "STEP_ACK payload")
    status = struct.unpack_from("<I", data)[0]
    _validate_status(status)
    if struct.unpack_from("<I", data, 12)[0] != 0:
        raise StepProtocolError("STEP_ACK reserved field is not zero")


def decode_step_ack_payload(data: bytes) -> StepAckPayload:
    """Decode and strictly validate a v2 STEP_ACK payload."""
    validate_step_ack_payload(data)
    status, queue_depth, dropped_count, _ = struct.unpack("<IIII", data)
    return StepAckPayload(status, queue_depth, dropped_count)


def encode_step_ack_payload(value: StepAckPayload) -> bytes:
    """Encode one native v2 STEP_ACK payload."""
    status = _uint(value.status, UINT32_MAX, "STEP_ACK status")
    _validate_status(status)
    queue_depth = _uint(value.queue_depth, UINT32_MAX,
                        "STEP_ACK queue depth")
    dropped_count = _uint(value.dropped_count, UINT32_MAX,
                          "STEP_ACK dropped count")
    return struct.pack("<IIII", status, queue_depth, dropped_count, 0)


def validate_diagnostics_payload(data: bytes) -> None:
    """Validate one native v2 DIAGNOSTICS payload."""
    _validate_payload_bytes(data, DIAGNOSTICS_PAYLOAD_SIZE,
                            "DIAGNOSTICS payload")


def decode_diagnostics_payload(data: bytes) -> DiagnosticsPayload:
    """Decode and strictly validate a v2 DIAGNOSTICS payload."""
    validate_diagnostics_payload(data)
    return DiagnosticsPayload(*struct.unpack("<QQQQQ", data))


def encode_diagnostics_payload(value: DiagnosticsPayload) -> bytes:
    """Encode one native v2 DIAGNOSTICS payload."""
    values = tuple(_uint(getattr(value, name), UINT64_MAX, name)
                   for name in ("rx_frames", "rx_bad_frames",
                                "rx_dropped_bytes", "tx_frames",
                                "tx_dropped"))
    return struct.pack("<QQQQQ", *values)


def validate_step_done_payload(data: bytes) -> None:
    """Validate one native v2 STEP_DONE payload."""
    _validate_payload_bytes(data, STEP_DONE_PAYLOAD_SIZE,
                            "STEP_DONE payload")
    status, consumed_mask, missing_mask, _, _ = struct.unpack(
        "<IIIII", data)
    _validate_status(status)
    if consumed_mask & ~V2_STEP_DONE_REQUIRED_MASK:
        raise StepProtocolError("STEP_DONE consumed mask has unsupported bits")
    if missing_mask & ~V2_STEP_DONE_REQUIRED_MASK:
        raise StepProtocolError("STEP_DONE missing mask has unsupported bits")
    if consumed_mask & missing_mask:
        raise StepProtocolError("STEP_DONE consumed and missing masks overlap")
    expected_missing = V2_STEP_DONE_REQUIRED_MASK & ~consumed_mask
    if missing_mask != expected_missing:
        raise StepProtocolError(
            "STEP_DONE missing mask does not match consumed mask")
    if status == V2_STATUS_OK and (
            consumed_mask != V2_STEP_DONE_REQUIRED_MASK or missing_mask != 0):
        raise StepProtocolError(
            "successful STEP_DONE must consume accel and gyro")


def decode_step_done_payload(data: bytes) -> StepDonePayload:
    """Decode and strictly validate a v2 STEP_DONE payload."""
    validate_step_done_payload(data)
    return StepDonePayload(*struct.unpack("<IIIII", data))


def encode_step_done_payload(value: StepDonePayload) -> bytes:
    """Encode one native v2 STEP_DONE payload."""
    status = _uint(value.status, UINT32_MAX, "STEP_DONE status")
    _validate_status(status)
    consumed_mask = _uint(value.consumed_mask, UINT32_MAX,
                          "STEP_DONE consumed mask")
    missing_mask = _uint(value.missing_mask, UINT32_MAX,
                         "STEP_DONE missing mask")
    queue_depth = _uint(value.queue_depth, UINT32_MAX,
                        "STEP_DONE queue depth")
    dropped_count = _uint(value.dropped_count, UINT32_MAX,
                          "STEP_DONE dropped count")
    payload = struct.pack("<IIIII", status, consumed_mask, missing_mask,
                          queue_depth, dropped_count)
    validate_step_done_payload(payload)
    return payload


def validate_board_telemetry_payload(data: bytes) -> None:
    """Validate the fixed v2 board-output telemetry payload."""
    _validate_payload_bytes(data, BOARD_TELEMETRY_PAYLOAD_SIZE,
                            "board telemetry payload")
    struct.unpack(BOARD_TELEMETRY_PAYLOAD_FORMAT, data)


def encode_board_telemetry_payload(
        telemetry: BoardTelemetryPayload) -> bytes:
    """Encode the native v2 board-output telemetry layout."""
    values = tuple(_uint(getattr(telemetry, name), UINT32_MAX, name)
                   for name in (
                       "led_rgb", "led_brightness", "buzzer", "board_flags",
                       "buzzer_frequency_hz", "buzzer_duty_permille"))
    return struct.pack(BOARD_TELEMETRY_PAYLOAD_FORMAT, *values)


def decode_board_telemetry_payload(data: bytes) -> BoardTelemetryPayload:
    """Decode and validate the native v2 board-output telemetry payload."""
    validate_board_telemetry_payload(data)
    return BoardTelemetryPayload(
        *struct.unpack(BOARD_TELEMETRY_PAYLOAD_FORMAT, data))


def _validate_kind(kind: int) -> int:
    kind = _uint(kind, UINT16_MAX, "kind")
    if kind not in (RESET, STEP, STEP_ACK, DIAGNOSTICS, RESET_ACK, STEP_DONE,
                    TELEMETRY, MOTOR_STATE):
        raise StepProtocolError(f"unsupported step message kind: {kind}")
    return kind


def _uint(value: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in range 0..{maximum}")
    return value


def _time(value: int, name: str) -> int:
    return _uint(value, UINT64_MAX, name)


def _float32(value: float, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a finite float")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite float")
    try:
        struct.pack("<f", value)
    except (OverflowError, struct.error) as exc:
        raise ValueError(f"{name} is outside binary32 range") from exc
    return value


def _vector(values: Sequence[float], size: int, name: str) -> tuple[float, ...]:
    if len(values) != size:
        raise ValueError(f"{name} must contain {size} values")
    return tuple(_float32(value, f"{name}[{i}]") for i, value in enumerate(values))


def _entries(entries: Iterable[object], count: int, name: str) -> tuple[object, ...]:
    values = tuple(entries)
    if len(values) != count:
        raise ValueError(f"{name} count does not match motor_count")
    seen: set[int] = set()
    for entry in values:
        index = _uint(getattr(entry, "index", -1), UINT16_MAX, "motor index")
        if index >= count or index in seen:
            raise ValueError("motor index must be unique and less than motor_count")
        seen.add(index)
    return values


def encode_step_header(header: StepHeader) -> bytes:
    version = _uint(header.version, UINT16_MAX, "version")
    if version != STEP_VERSION:
        raise ValueError("unsupported StepHeader version")
    kind = _uint(header.kind, UINT16_MAX, "kind")
    payload_len = _uint(header.payload_len, UINT32_MAX, "payload_len")
    step_id = _time(header.step_id, "step_id")
    t_sim_ns = _time(header.t_sim_ns, "t_sim_ns")
    dt_ns = _time(header.dt_ns, "dt_ns")
    flags = _uint(header.flags, UINT32_MAX, "flags")
    return struct.pack(STEP_HEADER_FORMAT, version, kind, payload_len,
                       step_id, t_sim_ns, dt_ns, flags)


def decode_step_header(data: bytes) -> StepHeader:
    if len(data) != STEP_HEADER_SIZE:
        raise ValueError("StepHeader must be exactly 36 bytes")
    try:
        values = struct.unpack(STEP_HEADER_FORMAT, data)
    except struct.error as exc:
        raise ValueError("invalid StepHeader") from exc
    header = StepHeader(*values)
    if header.version != STEP_VERSION:
        raise ValueError("unsupported StepHeader version")
    return header


def encode_step_frame(frame: StepFrame) -> bytes:
    """Encode one length-prefixed v2 frame.

    The outer length includes the magic and the fixed StepHeader, but excludes
    the four-byte length prefix itself.  This matches the v1 stream framing
    while keeping v2 parsing independent from the v1 header layout.
    """
    if not isinstance(frame.payload, (bytes, bytearray, memoryview)):
        raise StepProtocolError("frame payload must be bytes-like")
    payload = bytes(frame.payload)
    if len(payload) > STEP_MAX_PAYLOAD:
        raise StepProtocolError("step payload is too large")
    kind = _validate_kind(frame.header.kind)
    if frame.header.payload_len != len(payload):
        raise StepProtocolError("StepHeader payload_len does not match payload")
    header = StepHeader(STEP_VERSION, kind, len(payload),
                        frame.header.step_id, frame.header.t_sim_ns,
                        frame.header.dt_ns, frame.header.flags)
    body = struct.pack("<I", STEP_MAGIC) + encode_step_header(header) + payload
    return struct.pack("<I", len(body)) + body


def build_step_frame(kind: int, step_id: int, t_sim_ns: int,
                     dt_ns: int = 0, flags: int = 0,
                     payload: bytes = b"", *,
                     session_id: int | None = None) -> bytes:
    """Build a v2 frame, retaining ``flags`` as the wire session-ID slot.

    ``flags`` remains a positional-compatible alias for existing callers.
    New callers should use ``session_id``.  A zero ID selects legacy v2; a
    non-zero ID in RESET establishes the session and every later frame must
    carry the same value.  Supplying both names is allowed only when they
    agree.
    """
    if session_id is not None:
        _uint(session_id, UINT32_MAX, "session_id")
        if flags != 0 and flags != session_id:
            raise StepProtocolError("flags and session_id do not match")
        flags = session_id
    payload = bytes(payload)
    return encode_step_frame(StepFrame(
        StepHeader(STEP_VERSION, kind, len(payload), step_id,
                   t_sim_ns, dt_ns, flags), payload))


def decode_step_frame(data: bytes) -> StepFrame:
    """Decode exactly one complete length-prefixed v2 frame."""
    if len(data) < STEP_FRAME_PREFIX_SIZE + STEP_HEADER_SIZE:
        raise StepProtocolError("step frame is truncated")
    (body_len,) = struct.unpack_from("<I", data)
    if body_len < 4 + STEP_HEADER_SIZE or body_len > 4 + STEP_HEADER_SIZE + STEP_MAX_PAYLOAD:
        raise StepProtocolError("step frame has invalid length")
    if len(data) != STEP_OUTER_SIZE + body_len:
        raise StepProtocolError("step frame has trailing or missing bytes")
    if struct.unpack_from("<I", data, STEP_OUTER_SIZE)[0] != STEP_MAGIC:
        raise StepProtocolError("invalid step frame magic")
    header_offset = STEP_OUTER_SIZE + 4
    header = decode_step_header(data[header_offset:header_offset + STEP_HEADER_SIZE])
    _validate_kind(header.kind)
    payload = data[header_offset + STEP_HEADER_SIZE:]
    if header.payload_len != len(payload):
        raise StepProtocolError("step frame payload length mismatch")
    return StepFrame(header, payload)


def validate_step_frame(frame: StepFrame) -> None:
    """Validate frame-level invariants without changing session state."""
    encoded = encode_step_frame(frame)
    decoded = decode_step_frame(encoded)
    if decoded != frame:
        raise StepProtocolError("step frame is not canonical")


class StepSessionValidator:
    """Validate one direction of a v2 session.

        ``HOST_TO_QEMU`` validates the input stream (RESET, STEP and
    DIAGNOSTICS).  ``QEMU_TO_HOST`` validates responses independently: a
    STEP_ACK records a step and STEP_DONE must later close that exact step.
    The default ``AUTO`` mode retains the historical single-stream behavior
    for callers that used this class before directional validation existed.

    The 36-byte v2 header is intentionally unchanged: its ``flags``/u32
    field is the session ID slot.  The default validator adopts the ID from
    each RESET (including zero, which is legacy mode).  ``session_id`` pins
    validation to an explicit ID, while ``generate_session_id=True`` creates
    a non-zero ID that the caller must put in its RESET frame.  Once RESET is
        accepted, RESET_ACK, STEP, STEP_ACK, STEP_DONE, TELEMETRY,
        MOTOR_STATE, and DIAGNOSTICS must echo that same ID.

    In response mode ``require_step_done`` controls whether ACKs create a
    strict outstanding transaction.  The worker leaves it disabled by
    default so old QEMU endpoints that never emit STEP_DONE remain usable;
    ``--wait-step-done`` enables the strict lockstep mode.
    """

    def __init__(self, direction: str | StepSessionDirection = DIRECTION_AUTO,
                 *, require_step_done: bool = True,
                 session_id: int | None = None,
                 generate_session_id: bool = False) -> None:
        if isinstance(direction, StepSessionDirection):
            direction = direction.value
        aliases = {
            "tx": DIRECTION_HOST_TO_QEMU,
            "host": DIRECTION_HOST_TO_QEMU,
            "rx": DIRECTION_QEMU_TO_HOST,
            "qemu": DIRECTION_QEMU_TO_HOST,
        }
        direction = aliases.get(direction, direction)
        if direction not in (DIRECTION_AUTO, DIRECTION_HOST_TO_QEMU,
                             DIRECTION_QEMU_TO_HOST):
            raise ValueError("invalid step session direction")
        if session_id is not None:
            _uint(session_id, UINT32_MAX, "session_id")
        if generate_session_id and session_id is not None:
            raise ValueError("session_id and generate_session_id are exclusive")
        self.direction = direction
        self.require_step_done = bool(require_step_done)
        self._configured_session_id = session_id
        self._generated_session_id = (_generate_session_id()
                                      if generate_session_id else None)
        self.reset()

    def reset(self) -> None:
        self.initialized = False
        self._session_id = (self._generated_session_id
                            if self._generated_session_id is not None
                            else 0)
        self.last_step_id = 0
        self.last_t_sim_ns = 0
        self._reset_ack_pending = False
        self._reset_ack_seen = False
        self._last_ack_step_id = 0
        self._last_ack_t_sim_ns = 0
        self._last_ack_dt_ns = 0
        self._last_ack_status: int | None = None
        self._last_done_record: tuple[int, int, int] | None = None
        self._replayed_done_record: tuple[int, int, int] | None = None
        self._last_telemetry_step_id = 0
        self._last_telemetry_t_sim_ns = 0
        self._telemetry_seen = False
        self._pending_responses: list[tuple[int, int, int]] = []
        self._optional_ack_history: list[tuple[int, int, int]] = []
        self._optional_done_history: set[tuple[int, int]] = set()

    @property
    def session_id(self) -> int:
        """Current session ID; zero means the legacy v2 session."""
        return self._session_id

    def begin_reset(self, step_id: int, t_sim_ns: int,
                    session_id: int = 0) -> None:
        """Set the expected response baseline for a host RESET.

        This is used by a QEMU->host validator after the host has sent RESET;
        the RESET frame itself belongs to the opposite directional validator.
        """
        _time(step_id, "step_id")
        _time(t_sim_ns, "t_sim_ns")
        _uint(session_id, UINT32_MAX, "session_id")
        self._validate_reset_session_id(session_id)
        self.reset()
        self._session_id = session_id
        self.initialized = True
        self.last_step_id = step_id
        self.last_t_sim_ns = t_sim_ns
        self._reset_ack_pending = True

    def _validate_reset_session_id(self, session_id: int) -> None:
        expected = self._configured_session_id
        if expected is None:
            expected = self._generated_session_id
        if expected is not None and session_id != expected:
            raise StepProtocolError(
                "RESET session_id does not match validator session")

    def _accept_session_id(self, header: StepHeader) -> None:
        if header.kind == RESET:
            self._validate_reset_session_id(header.session_id)
            return
        if not self.initialized:
            return
        if header.session_id != self._session_id:
            raise StepProtocolError(
                "frame session_id does not match active session")

    @classmethod
    def host_to_qemu(cls, **kwargs) -> "StepSessionValidator":
        return cls(DIRECTION_HOST_TO_QEMU, **kwargs)

    @classmethod
    def qemu_to_host(cls, **kwargs) -> "StepSessionValidator":
        return cls(DIRECTION_QEMU_TO_HOST, **kwargs)

    def _start_reset(self, step_id: int, t_sim_ns: int,
                     session_id: int) -> None:
        self.initialized = True
        # RESET is the only frame allowed to change the active session.
        # ``_accept_session_id`` has already checked configured/generated IDs.
        # The default mode therefore adopts a new non-zero ID here while
        # preserving zero as the legacy v2 mode.
        self._session_id = session_id
        self.last_step_id = step_id
        self.last_t_sim_ns = t_sim_ns
        self._reset_ack_pending = True
        self._reset_ack_seen = False
        self._last_ack_step_id = step_id
        self._last_ack_t_sim_ns = t_sim_ns
        self._last_ack_dt_ns = 0
        self._last_ack_status = None
        self._last_done_record = None
        self._replayed_done_record = None
        self._last_telemetry_step_id = 0
        self._last_telemetry_t_sim_ns = 0
        self._telemetry_seen = False
        self._pending_responses.clear()
        self._optional_ack_history.clear()
        self._optional_done_history.clear()

    def _accept_monotonic_input(self, header: StepHeader) -> None:
        if header.step_id <= self.last_step_id:
            raise StepProtocolError("step_id must increase strictly")
        if header.t_sim_ns <= self.last_t_sim_ns:
            raise StepProtocolError("t_sim_ns must increase strictly")
        if header.kind == STEP:
            if header.dt_ns == 0:
                raise StepProtocolError("STEP requires non-zero dt_ns")
            if header.dt_ns != header.t_sim_ns - self.last_t_sim_ns:
                raise StepProtocolError(
                    "STEP dt_ns must equal the t_sim_ns increment")
        self.last_step_id = header.step_id
        self.last_t_sim_ns = header.t_sim_ns

    def _accept_host_to_qemu(self, header: StepHeader,
                             payload: bytes) -> None:
        if header.kind == RESET:
            self._start_reset(header.step_id, header.t_sim_ns,
                              header.session_id)
            return
        if not self.initialized:
            raise StepProtocolError("step session requires RESET first")
        if header.kind not in (STEP, DIAGNOSTICS):
            raise StepProtocolError(
                "response frame is not valid in host-to-QEMU session")
        if header.kind == STEP:
            validate_step_input_payload(payload)
        self._accept_monotonic_input(header)

    def _accept_qemu_to_host(self, header: StepHeader,
                             payload: bytes) -> None:
        if header.kind == RESET:
            self._start_reset(header.step_id, header.t_sim_ns,
                              header.session_id)
            return
        if not self.initialized:
            raise StepProtocolError("step session requires RESET first")
        if header.kind == RESET_ACK:
            decode_reset_ack_payload(payload)
            if (not self._reset_ack_pending or self._reset_ack_seen or
                    header.step_id != self.last_step_id or
                    header.t_sim_ns != self.last_t_sim_ns):
                raise StepProtocolError("RESET_ACK does not match reset baseline")
            self._reset_ack_seen = True
            self._reset_ack_pending = False
            return
        if self._reset_ack_pending:
            raise StepProtocolError("response session requires RESET_ACK first")
        if header.kind == TELEMETRY:
            decode_board_telemetry_payload(payload)
            if (self._telemetry_seen and
                    (header.step_id <= self._last_telemetry_step_id or
                     header.t_sim_ns <= self._last_telemetry_t_sim_ns)):
                raise StepProtocolError("telemetry response is out of order")
            self._last_telemetry_step_id = header.step_id
            self._last_telemetry_t_sim_ns = header.t_sim_ns
            self._telemetry_seen = True
            return
        if header.kind == MOTOR_STATE:
            decode_motor_state(payload)
            record = (header.step_id, header.t_sim_ns, header.dt_ns)
            if self._last_ack_status != V2_STATUS_OK:
                raise StepProtocolError(
                    "MOTOR_STATE requires a successful STEP_ACK")
            if record != (self._last_ack_step_id,
                          self._last_ack_t_sim_ns,
                          self._last_ack_dt_ns):
                raise StepProtocolError(
                    "MOTOR_STATE does not match the latest STEP_ACK")
            return
        if header.kind == STEP_ACK:
            if header.dt_ns == 0:
                raise StepProtocolError("STEP_ACK requires non-zero dt_ns")
            ack = decode_step_ack_payload(payload)
            record = (header.step_id, header.t_sim_ns, header.dt_ns)
            same_ack = (record == (self._last_ack_step_id,
                                   self._last_ack_t_sim_ns,
                                   self._last_ack_dt_ns))
            if same_ack and self._last_ack_status is not None:
                previous_status = self._last_ack_status
                if ack.status == previous_status:
                    # Every terminal status is idempotently replayable. An
                    # OK replay also permits QEMU's paired DONE replay.
                    if ack.status == V2_STATUS_OK and self.require_step_done:
                        self._replayed_done_record = record
                    return
                if (previous_status != V2_STATUS_QUEUE_FULL or
                        ack.status != V2_STATUS_OK):
                    raise StepProtocolError(
                        "STEP_ACK terminal status changed for the same step")
                # QUEUE_FULL is provisional and may transition to OK.
            elif (header.step_id <= self._last_ack_step_id or
                  header.t_sim_ns <= self._last_ack_t_sim_ns or
                  header.dt_ns != header.t_sim_ns -
                  self._last_ack_t_sim_ns):
                raise StepProtocolError(
                    "STEP_ACK step_id and t_sim_ns must increase strictly")
            self._last_ack_step_id, self._last_ack_t_sim_ns = record[:2]
            self._last_ack_dt_ns = header.dt_ns
            self._last_ack_status = ack.status
            self.last_step_id = header.step_id
            self.last_t_sim_ns = header.t_sim_ns
            if ack.status == V2_STATUS_OK:
                if self.require_step_done:
                    self._pending_responses.append(record)
                else:
                    self._optional_ack_history.append(record)
                    # Optional mode only needs bounded late-DONE matching;
                    # an endpoint that never emits DONE must not leak memory.
                    if len(self._optional_ack_history) > 1024:
                        self._optional_ack_history.pop(0)
            return
        if header.kind == STEP_DONE:
            done = decode_step_done_payload(payload)
            record = (header.step_id, header.t_sim_ns, header.dt_ns)
            if self.require_step_done:
                if not self._pending_responses:
                    if (record == self._replayed_done_record and
                            record == self._last_done_record):
                        # QEMU replays ACK + DONE for the most recent
                        # completed STEP when the original DONE may have been
                        # lost. Accept exactly that one replay, while an
                        # isolated duplicate DONE remains an error.
                        self._replayed_done_record = None
                        return
                    raise StepProtocolError(
                        "STEP_DONE has no outstanding STEP_ACK")
                expected = self._pending_responses[0]
                if record != expected:
                    raise StepProtocolError(
                        "STEP_DONE is duplicated or out of order")
                self._pending_responses.pop(0)
                self._last_done_record = record
                if self._replayed_done_record == record:
                    self._replayed_done_record = None
            else:
                if record not in self._optional_ack_history:
                    raise StepProtocolError(
                        "STEP_DONE has no matching STEP_ACK")
                key = (header.step_id, header.t_sim_ns)
                if key in self._optional_done_history:
                    raise StepProtocolError("duplicate STEP_DONE")
                self._optional_done_history.add(key)
            return
        if header.kind == DIAGNOSTICS:
            decode_diagnostics_payload(payload)
            if (header.step_id < self._last_ack_step_id or
                    (header.step_id == self._last_ack_step_id and
                     header.t_sim_ns < self._last_ack_t_sim_ns)):
                raise StepProtocolError("diagnostics response is out of order")
            return
        raise StepProtocolError(
            "input frame is not valid in QEMU-to-host session")

    def _accept_auto(self, header: StepHeader, payload: bytes) -> None:
        # Compatibility path for the original mixed-direction API. It still
        # understands the two-phase response, but directional instances above
        # provide the strict contract for new callers.
        if header.kind == RESET:
            self._start_reset(header.step_id, header.t_sim_ns,
                              header.session_id)
            return
        if not self.initialized:
            raise StepProtocolError("step session requires RESET first")
        if (header.kind == RESET_ACK and
                header.step_id == self.last_step_id and
                header.t_sim_ns == self.last_t_sim_ns):
            if self._reset_ack_seen:
                raise StepProtocolError("duplicate RESET_ACK")
            self._reset_ack_seen = True
            return
        if header.kind == STEP_DONE:
            decode_step_done_payload(payload)
            if (header.step_id == self._last_ack_step_id and
                    header.t_sim_ns == self._last_ack_t_sim_ns and
                    header.dt_ns == self._last_ack_dt_ns):
                if self._reset_ack_pending:
                    raise StepProtocolError("STEP_DONE before STEP_ACK")
                if (header.step_id, header.t_sim_ns) in self._optional_done_history:
                    raise StepProtocolError("duplicate STEP_DONE")
                self._optional_done_history.add((header.step_id,
                                                  header.t_sim_ns))
                return
        if header.kind == MOTOR_STATE:
            decode_motor_state(payload)
            if (header.step_id == self._last_ack_step_id and
                    header.t_sim_ns == self._last_ack_t_sim_ns and
                    header.dt_ns == self._last_ack_dt_ns):
                return
        if header.step_id <= self.last_step_id:
            raise StepProtocolError("step_id must increase strictly")
        if header.t_sim_ns <= self.last_t_sim_ns:
            raise StepProtocolError("t_sim_ns must increase strictly")
        if header.kind in (STEP, STEP_ACK) and header.dt_ns == 0:
            raise StepProtocolError("STEP and STEP_ACK require non-zero dt_ns")
        if header.kind == STEP_ACK:
            self._last_ack_step_id = header.step_id
            self._last_ack_t_sim_ns = header.t_sim_ns
            self._last_ack_dt_ns = header.dt_ns
        self.last_step_id = header.step_id
        self.last_t_sim_ns = header.t_sim_ns

    def accept(self, frame: StepFrame) -> None:
        validate_step_frame(frame)
        header = frame.header
        self._accept_session_id(header)
        if header.kind == STEP_DONE:
            validate_step_done_payload(frame.payload)
        if self.direction == DIRECTION_HOST_TO_QEMU:
            self._accept_host_to_qemu(header, frame.payload)
        elif self.direction == DIRECTION_QEMU_TO_HOST:
            self._accept_qemu_to_host(header, frame.payload)
        else:
            self._accept_auto(header, frame.payload)


def encode_motor_command(command: MotorCommand) -> bytes:
    count = _uint(command.motor_count, UINT16_MAX, "motor_count")
    flags = _uint(command.flags, UINT16_MAX, "flags")
    entries = _entries(command.commands, count, "command")
    output = bytearray(struct.pack("<HH", count, flags))
    for entry in entries:
        output += struct.pack("<Hf", entry.index,
                              _float32(entry.command, "command"))
    return bytes(output)


def decode_motor_command(data: bytes) -> MotorCommand:
    if len(data) < 4:
        raise ValueError("MotorCommand is truncated")
    count, flags = struct.unpack_from("<HH", data)
    expected = 4 + count * 6
    if len(data) != expected:
        raise ValueError("MotorCommand has invalid length")
    entries = tuple(MotorCommandItem(*struct.unpack_from("<Hf", data, 4 + i * 6))
                    for i in range(count))
    _entries(entries, count, "command")
    for entry in entries:
        _float32(entry.command, "command")
    return MotorCommand(count, flags, entries)


def encode_motor_state(state: MotorState) -> bytes:
    count = _uint(state.motor_count, UINT16_MAX, "motor_count")
    entries = _entries(state.states, count, "state")
    output = bytearray(struct.pack("<H", count))
    for entry in entries:
        output += struct.pack("<Hfff", entry.index,
                              _float32(entry.position, "position"),
                              _float32(entry.velocity, "velocity"),
                              _float32(entry.effort, "effort"))
    return bytes(output)


def decode_motor_state(data: bytes) -> MotorState:
    if len(data) < 2:
        raise ValueError("MotorState is truncated")
    (count,) = struct.unpack_from("<H", data)
    expected = 2 + count * 14
    if len(data) != expected:
        raise ValueError("MotorState has invalid length")
    entries = tuple(MotorStateItem(*struct.unpack_from("<Hfff", data, 2 + i * 14))
                    for i in range(count))
    _entries(entries, count, "state")
    for entry in entries:
        for value, name in ((entry.position, "position"),
                            (entry.velocity, "velocity"),
                            (entry.effort, "effort")):
            _float32(value, name)
    return MotorState(count, entries)


def encode_imu_sample_v2(sample: ImuSampleV2) -> bytes:
    gyro = _vector(sample.gyro_dps, 3, "gyro_dps")
    accel = _vector(sample.accel_g, 3, "accel_g")
    gyro_bias = _vector(sample.gyro_bias_dps, 3, "gyro_bias_dps")
    accel_bias = _vector(sample.accel_bias_g, 3, "accel_bias_g")
    sensor_time = _time(sample.sensor_time_ns, "sensor_time_ns")
    sample_id = _uint(sample.sample_id, UINT32_MAX, "sample_id")
    return struct.pack("<12fQI", *(gyro + accel + gyro_bias + accel_bias),
                       sensor_time, sample_id)


def decode_imu_sample_v2(data: bytes) -> ImuSampleV2:
    if len(data) != 60:
        raise ValueError("ImuSampleV2 must be exactly 60 bytes")
    values = struct.unpack("<12fQI", data)
    floats = values[:12]
    for value in floats:
        _float32(value, "IMU value")
    return ImuSampleV2(tuple(floats[0:3]), tuple(floats[3:6]),
                       tuple(floats[6:9]), tuple(floats[9:12]),
                       _time(values[12], "sensor_time_ns"), values[13])


def encode_step_payload(sections: Iterable[StepSection]) -> bytes:
    """Encode an extensible v2 STEP section list."""
    values = tuple(sections)
    if len(values) > UINT16_MAX:
        raise StepProtocolError("too many step payload sections")
    output = bytearray(struct.pack(STEP_PAYLOAD_HEADER_FORMAT, len(values),
                                   STEP_PAYLOAD_SECTION_MARKER))
    for section in values:
        if not isinstance(section.payload, (bytes, bytearray, memoryview)):
            raise StepProtocolError("step section payload must be bytes-like")
        payload = bytes(section.payload)
        section_type = _uint(section.type, UINT16_MAX, "section type")
        flags = _uint(section.flags, UINT16_MAX, "section flags")
        if section_type == 0:
            raise StepProtocolError("section type must be non-zero")
        if flags != 0:
            raise StepProtocolError("step section flags are reserved and must be zero")
        output += struct.pack(STEP_SECTION_HEADER_FORMAT, section_type,
                              flags, len(payload))
        output += payload
    if len(output) > STEP_PAYLOAD_MAX:
        raise StepProtocolError("step payload is too large")
    return bytes(output)


def decode_step_payload(data: bytes) -> tuple[StepSection, ...]:
    """Decode and structurally validate an extensible v2 STEP payload."""
    if len(data) < STEP_PAYLOAD_HEADER_SIZE or len(data) > STEP_PAYLOAD_MAX:
        raise StepProtocolError("invalid step payload length")
    count, marker = struct.unpack_from(STEP_PAYLOAD_HEADER_FORMAT, data)
    if marker != STEP_PAYLOAD_SECTION_MARKER:
        if len(data) == 60:
            # The old format has no discriminator. A 60-byte payload is
            # therefore owned by compact IMU decoding.
            raise StepProtocolError(
                "60-byte step payload is compact legacy IMU, not section data")
        if marker != 0:
            raise StepProtocolError("step payload section marker is invalid")
    offset = STEP_PAYLOAD_HEADER_SIZE
    sections: list[StepSection] = []
    for _ in range(count):
        if offset + STEP_SECTION_HEADER_SIZE > len(data):
            raise StepProtocolError("truncated step section header")
        section_type, flags, payload_len = struct.unpack_from(
            STEP_SECTION_HEADER_FORMAT, data, offset)
        offset += STEP_SECTION_HEADER_SIZE
        if (section_type == 0 or flags != 0 or
                payload_len > len(data) - offset):
            if flags != 0:
                raise StepProtocolError(
                    "step section flags are reserved and must be zero")
            raise StepProtocolError("invalid step section length")
        payload = bytes(data[offset:offset + payload_len])
        offset += payload_len
        sections.append(StepSection(section_type, flags, payload))
    if offset != len(data):
        raise StepProtocolError("step payload has trailing bytes")
    return tuple(sections)


def validate_step_input_payload(data: bytes) -> None:
    """Validate the complete typed payload accepted by a STEP input."""
    if (len(data) == 60 and
            struct.unpack_from("<H", data, 2)[0] !=
            STEP_PAYLOAD_SECTION_MARKER):
        decode_imu_sample_v2(data)
        return
    sections = decode_step_payload(data)
    if not sections:
        raise StepProtocolError("STEP payload must contain at least one section")

    seen_singletons: set[int] = set()
    seen_adc_inputs: set[int] = set()
    seen_adc_voltages: set[int] = set()
    for section in sections:
        section_type = section.type
        singleton: int | None = None
        if section_type == STEP_SECTION_IMU_SAMPLE:
            decode_imu_sample_v2(section.payload)
            singleton = section_type
        elif section_type == STEP_SECTION_MOTOR_COMMAND:
            decode_motor_command(section.payload)
            singleton = section_type
        elif section_type == STEP_SECTION_ADC_INPUT:
            value = decode_adc_input_v2(section.payload)
            if value.channel in seen_adc_inputs:
                raise StepProtocolError("duplicate ADC input channel")
            seen_adc_inputs.add(value.channel)
        elif section_type == STEP_SECTION_ADC_VOLTAGE:
            value = decode_adc_voltage_v2(section.payload)
            if value.channel in seen_adc_voltages:
                raise StepProtocolError("duplicate ADC voltage channel")
            seen_adc_voltages.add(value.channel)
        elif section_type == STEP_SECTION_MOTOR_STATE:
            raise StepProtocolError("MOTOR_STATE is not valid in STEP input")
        else:
            raise StepProtocolError(f"unknown STEP section type: {section_type}")
        if singleton is not None:
            if singleton in seen_singletons:
                raise StepProtocolError("duplicate STEP section")
            seen_singletons.add(singleton)


def encode_adc_input_v2(value: AdcInputV2) -> bytes:
    channel = _uint(value.channel, 31, "ADC channel")
    raw = _uint(value.raw, UINT16_MAX, "ADC raw value")
    return struct.pack("<HHI", channel, raw, 0)


def decode_adc_input_v2(data: bytes) -> AdcInputV2:
    if len(data) != 8:
        raise ValueError("AdcInputV2 must be exactly 8 bytes")
    channel, raw, reserved = struct.unpack("<HHI", data)
    if channel > 31 or reserved != 0:
        raise ValueError("invalid AdcInputV2")
    return AdcInputV2(channel, raw)


def encode_adc_voltage_v2(value: AdcVoltageV2) -> bytes:
    channel = _uint(value.channel, 31, "ADC channel")
    flags = _uint(value.flags, UINT16_MAX, "ADC voltage flags")
    if flags & ~1:
        raise ValueError("unsupported ADC voltage flags")
    voltage_uv = _uint(value.voltage_uv, 3_300_000, "ADC voltage")
    return struct.pack("<HHII", channel, flags, voltage_uv, 0)


def decode_adc_voltage_v2(data: bytes) -> AdcVoltageV2:
    if len(data) != 12:
        raise ValueError("AdcVoltageV2 must be exactly 12 bytes")
    channel, flags, voltage_uv, reserved = struct.unpack("<HHII", data)
    if channel > 31 or flags & ~1 or voltage_uv > 3_300_000 or reserved != 0:
        raise ValueError("invalid AdcVoltageV2")
    return AdcVoltageV2(channel, voltage_uv, flags)


def build_step_input(imu: ImuSampleV2,
                     adc_inputs: Iterable[AdcInputV2] = (),
                     adc_voltages: Iterable[AdcVoltageV2] = (),
                     motor_command: MotorCommand | None = None) -> bytes:
    """Build a STEP payload with an IMU sample and optional input sections."""
    sections = [StepSection(STEP_SECTION_IMU_SAMPLE, 0,
                            encode_imu_sample_v2(imu))]
    sections.extend(StepSection(STEP_SECTION_ADC_INPUT, 0,
                                encode_adc_input_v2(value))
                    for value in adc_inputs)
    sections.extend(StepSection(STEP_SECTION_ADC_VOLTAGE, 0,
                                encode_adc_voltage_v2(value))
                    for value in adc_voltages)
    if motor_command is not None:
        sections.append(StepSection(STEP_SECTION_MOTOR_COMMAND, 0,
                                    encode_motor_command(motor_command)))
    return encode_step_payload(sections)


# Short aliases are useful to callers that use pack/unpack terminology.
pack_step_header = encode_step_header
unpack_step_header = decode_step_header
pack_motor_command = encode_motor_command
unpack_motor_command = decode_motor_command
pack_motor_state = encode_motor_state
unpack_motor_state = decode_motor_state
pack_imu_sample_v2 = encode_imu_sample_v2
unpack_imu_sample_v2 = decode_imu_sample_v2
unpack_reset_ack_payload = decode_reset_ack_payload
unpack_step_ack_payload = decode_step_ack_payload
unpack_diagnostics_payload = decode_diagnostics_payload
unpack_step_done_payload = decode_step_done_payload
pack_reset_ack_payload = encode_reset_ack_payload
pack_step_ack_payload = encode_step_ack_payload
pack_diagnostics_payload = encode_diagnostics_payload
pack_step_done_payload = encode_step_done_payload
pack_step_frame = encode_step_frame
unpack_step_frame = decode_step_frame
