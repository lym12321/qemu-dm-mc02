"""Independent board exchanges around one owned joint world.

The runtime borrows preconstructed endpoints and their nonblocking streams.
One Coordinator/CanParticipant owns the existing plant; this is a synchronous
host composition, not a CPU target barrier. Any operation failure is terminal:
recreate the complete owner and board processes, never replay a world tick.
Callbacks may observe state but must not reenter execution or cleanup.
"""

from dataclasses import dataclass
import math
import select
import time

from sim.core import Coordinator, Session
from sim.devices.bmi088 import imu_to_bmi088_units
from sim.devices.dji import M3508JointPlant
from sim.fabric import CanFrame
from sim.fabric.can import check_can_time
from sim.fabric.endpoints import QemuCanEndpoint
from sim.fabric.runtime import CanParticipant
from sim.transports.can_stream import CanStream
from sim.transports.cosim import (
    QemuTimestampMapper, StepFramedSocket, V2SessionReset,
    poll_v2_response, send_v2_step, wait_v2_response,
)
from sim.transports import step_codec as codec


@dataclass(frozen=True)
class BoardBinding:
    name: str
    endpoint: QemuCanEndpoint
    cosim: StepFramedSocket
    imu: str


def _finite_seconds(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


class SharedWorldRuntime:
    """Own one plant, borrowing an ordered plain tuple of board bindings.

    ``on_response(name, frame, decoded)`` sees returned RESET/STEP ACKs and
    idle observations, including actual advisory DONE masks/status. Existing
    active waits may skip advisory frames; this is not a complete frame trace.
    Absolute host deadlines raise RuntimeError so the canonical STEP helper
    cannot swallow them as an internal retry timeout. A TimeoutError from wait
    servicing also becomes RuntimeError with the original exception as cause;
    only the helper's own ACK timeout may trigger retry. Native dependencies must
    return: this synchronous owner cannot preempt a blocked solver/callback.
    """

    def __init__(self, plant: M3508JointPlant,
                 bindings: tuple[BoardBinding, ...], *,
                 check_running=None, on_response=None):
        if not isinstance(plant, M3508JointPlant) or plant.closed:
            raise TypeError("shared world requires an open M3508 joint plant")
        if (type(bindings) is not tuple or not bindings
                or any(type(item) is not BoardBinding for item in bindings)):
            raise TypeError("shared world requires a nonempty plain BoardBinding tuple")
        for callback, name in ((check_running, "check_running"),
                               (on_response, "on_response")):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or None")
        for item in bindings:
            if (not isinstance(item.name, str) or not item.name or "\0" in item.name
                    or not isinstance(item.imu, str) or not item.imu or "\0" in item.imu):
                raise ValueError("board names and IMU bindings must be nonempty without NUL")
            if not isinstance(item.endpoint, QemuCanEndpoint):
                raise TypeError("board endpoint requires QemuCanEndpoint")
            if not isinstance(item.endpoint.stream, CanStream):
                raise TypeError("board CAN requires the canonical CanStream")
            if not isinstance(item.cosim, StepFramedSocket):
                raise TypeError("board co-sim requires StepFramedSocket")
        buses = tuple(item.endpoint.bus for item in bindings)
        streams = tuple(stream for item in bindings
                        for stream in (item.endpoint.stream, item.cosim))
        if len({item.name for item in bindings}) != len(bindings):
            raise ValueError("each board name must appear once")
        if len({id(bus) for bus in buses}) != len(buses):
            raise ValueError("each board CAN bus must appear once")
        if len({id(stream) for stream in streams}) != len(streams):
            raise ValueError("each borrowed stream must appear once")
        sockets = tuple(stream.sock for stream in streams)
        if (any(sock.fileno() < 0 or sock.gettimeout() != 0 for sock in sockets)
                or len({sock.fileno() for sock in sockets}) != len(sockets)):
            raise ValueError("borrowed sockets must be open, nonblocking and unique")
        if (any(device.closed for device in plant.devices)
                or {id(device.bus) for device in plant.devices} != {id(bus) for bus in buses}):
            raise ValueError("open plant devices must represent exactly the board buses")
        getter = getattr(plant.physics, "get_imu", None)
        if not callable(getter):
            raise TypeError("shared world physics requires get_imu")
        # Getters are public, completed-state observations. No wiring, reset,
        # solver, transport or ownership effect occurs before full admission.
        for item in bindings:
            gyro, accel = imu_to_bmi088_units(getter(item.imu))
            codec.encode_imu_sample_v2(codec.ImuSampleV2(
                gyro, accel, (0.0,) * 3, (0.0,) * 3, 0, 1))
        coordinator = Coordinator({"can": CanParticipant(buses, (plant,))})
        self.bindings = bindings
        self.plant = plant
        self._coordinator = coordinator
        self._check_running = check_running
        self._on_response = on_response
        self._attempted = False
        self._failed = True
        self._busy = False
        self._rx = ()
        self._tx = ()
        self._mappers = ()

    @property
    def t_sim_ns(self):
        return self._coordinator.t_sim_ns

    @property
    def step_id(self):
        return self._coordinator.step_id

    @property
    def failed(self):
        return self._failed

    @property
    def closed(self):
        return self._coordinator.closed

    def _guard(self, deadline):
        if self._check_running is not None:
            self._check_running()
        if time.monotonic() >= deadline:
            raise RuntimeError("shared world host deadline expired")

    def _observe(self, index, frame, decoded):
        if self._on_response is not None:
            self._on_response(self.bindings[index].name, frame, decoded)

    def _poll(self, index):
        response = poll_v2_response(self.bindings[index].cosim, self._rx[index])
        if response is None:
            return
        frame, decoded = response
        self._observe(index, frame, decoded)
        if frame.header.kind == codec.RESET:
            raise V2SessionReset(frame.header.step_id, frame.header.t_sim_ns,
                                 frame.header.session_id)

    def _service(self, deadline, timeout, *, active=None, acked=None):
        self._guard(deadline)
        # During start, only already-ACKed peers may be polled. Other RESET_ACKs
        # remain physically queued for their exclusive active wait. CAN RX is
        # also held until every origin is bound and the sole world reset ends.
        idle = (tuple(i for i in range(len(self.bindings)) if i != active)
                if acked is None else tuple(i for i in acked if i != active))
        for index in idle:
            self._poll(index)
            self._guard(deadline)
        readable = [item.cosim.sock for item in self.bindings]
        writable = [item.cosim.sock for item in self.bindings if item.cosim.tx_pending]
        if acked is None:
            readable.extend(item.endpoint.stream.sock for item in self.bindings)
            writable.extend(item.endpoint.stream.sock for item in self.bindings
                            if item.endpoint.stream.tx_pending)
        _, ready_write, _ = select.select(
            readable, writable, [], min(timeout, max(0, deadline - time.monotonic())))
        self._guard(deadline)
        for item in self.bindings:
            if item.cosim.sock in ready_write:
                item.cosim.flush()
            if acked is None and item.endpoint.stream.sock in ready_write:
                item.endpoint.stream.flush()
            self._guard(deadline)
        # One canonical CAN read per stream; parsing/ingress never advances
        # time. The bus retains original ordering/future delivery and queues.
        if acked is None:
            for item in self.bindings:
                for can_id, flags, dlc, timestamp, data in item.endpoint.stream.receive():
                    item.endpoint.ingest(CanFrame(can_id, flags, dlc, data, timestamp),
                                         self.t_sim_ns)
                self._guard(deadline)

    def _wait_service(self, deadline, timeout, active):
        try:
            self._service(deadline, timeout, active=active)
        except TimeoutError as exc:
            # The canonical STEP helper retries its own ACK wait timeouts.
            # Callback/transport failures are terminal, even if transient.
            raise RuntimeError("shared world wait service failed") from exc

    def start(self, session: Session, wire_sessions: tuple[int, ...], *, deadline):
        """Return all real RESET_ACKs, then bind epochs and reset the world once."""
        if self.closed or self._busy or self._attempted:
            raise RuntimeError("shared world start requires a fresh owner")
        self._attempted = True
        self._busy = True
        try:
            deadline = _finite_seconds(deadline, "deadline")
            if not isinstance(session, Session):
                raise TypeError("shared world start requires a Session")
            check_can_time(session.t_sim_ns)
            if (type(wire_sessions) is not tuple
                    or len(wire_sessions) != len(self.bindings)
                    or any(type(value) is not int or not 0 < value <= codec.UINT32_MAX
                           for value in wire_sessions)):
                raise ValueError("wire sessions must be a matching plain tuple of nonzero u32 IDs")
            resets = tuple(codec.build_step_frame(
                codec.RESET, 0, session.t_sim_ns, session_id=value)
                for value in wire_sessions)
            tx = tuple(codec.StepSessionValidator.host_to_qemu() for _ in self.bindings)
            rx = tuple(codec.StepSessionValidator.qemu_to_host(require_step_done=False)
                       for _ in self.bindings)
            for validator_tx, validator_rx, wire, value in zip(tx, rx, resets, wire_sessions):
                validator_tx.accept(codec.decode_step_frame(wire))
                validator_rx.begin_reset(0, session.t_sim_ns, value)
            self._tx, self._rx = tx, rx
            self._guard(deadline)
            for item, wire in zip(self.bindings, resets):
                self._guard(deadline)
                item.cosim.send(wire)
                self._guard(deadline)
            responses = []
            mappers = []
            for index, item in enumerate(self.bindings):
                self._guard(deadline)
                response = wait_v2_response(
                    item.cosim, rx[index], codec.RESET_ACK, 0, session.t_sim_ns,
                    timeout=max(0, deadline - time.monotonic()), interrupt_on_reset=True,
                    service_io=lambda timeout, active=index: self._service(
                        deadline, timeout, active=active, acked=range(len(responses))))
                self._guard(deadline)
                ack = codec.decode_reset_ack_payload(response.payload)
                required = codec.CAP_STEP | codec.CAP_IMU
                if ack.capabilities & required != required:
                    raise RuntimeError("board RESET_ACK lacks STEP/IMU capability")
                mapper = QemuTimestampMapper(ack.virtual_time_ns, session.t_sim_ns)
                check_can_time(mapper.qemu_origin_ns)
                responses.append(response)
                mappers.append(mapper)
                self._observe(index, response, ack)
                self._guard(deadline)
            self._mappers = tuple(mappers)
            self._service(deadline, 0, acked=range(len(self.bindings)))
            for item, mapper in zip(self.bindings, mappers):
                item.endpoint.bind_mapper(mapper)
            self._guard(deadline)
            self._coordinator.reset(session)
            self._guard(deadline)
            self._failed = False
            return tuple(responses)
        finally:
            self._busy = False

    def _enter(self):
        if self.closed or self._busy or self.failed:
            raise RuntimeError("shared world requires a successful start and fresh runtime")
        self._busy = True
        self._failed = True

    def service(self, *, deadline, timeout=0):
        """Service one bounded pass without advancing the world or borrowed clocks."""
        self._enter()
        try:
            deadline = _finite_seconds(deadline, "deadline")
            timeout = _finite_seconds(timeout, "timeout")
            if timeout < 0:
                raise ValueError("timeout must be nonnegative")
            self._service(deadline, timeout)
            self._guard(deadline)
            self._failed = False
        finally:
            self._busy = False

    def step(self, dt_ns: int, *, deadline):
        """Advance the sole world once; return actual per-board STEP_ACKs."""
        self._enter()
        try:
            deadline = _finite_seconds(deadline, "deadline")
            if type(dt_ns) is not int or not 0 < dt_ns <= 0x7FFFFFFFFFFFFFFF:
                raise ValueError("dt_ns must be a positive signed64 integer")
            target = self.t_sim_ns + dt_ns
            check_can_time(target)
            next_step = self.step_id + 1
            if next_step > codec.UINT32_MAX:
                raise ValueError("STEP sample ID must fit the unsigned 32-bit field")
            for item in self.bindings:
                check_can_time(target + item.endpoint.bus.delay_ns)
            for mapper in self._mappers:
                check_can_time(mapper.unmap(target, mapper.qemu_origin_ns))
            self._guard(deadline)
            self._service(deadline, 0)
            self._guard(deadline)
            self._coordinator.step(dt_ns)
            self._guard(deadline)
            # Collect every completed IMU and encode every outgoing frame
            # before sending any STEP; a later bad source cannot publish half
            # the board group. CAN/native effects already performed are retained.
            frames = []
            for item, validator in zip(self.bindings, self._tx):
                self._guard(deadline)
                gyro, accel = imu_to_bmi088_units(self.plant.physics.get_imu(item.imu))
                payload = codec.encode_imu_sample_v2(codec.ImuSampleV2(
                    gyro, accel, (0.0,) * 3, (0.0,) * 3, target, next_step))
                wire = codec.build_step_frame(codec.STEP, next_step, target, dt_ns,
                                             session_id=validator.session_id, payload=payload)
                frames.append((codec.decode_step_frame(wire), wire))
                self._guard(deadline)
            for validator, (frame, _) in zip(self._tx, frames):
                validator.accept(frame)
            responses = []
            for index, (item, (_, wire)) in enumerate(zip(self.bindings, frames)):
                self._guard(deadline)
                response = send_v2_step(
                    item.cosim, self._rx[index], wire, next_step, target,
                    service_io=lambda timeout, active=index: self._wait_service(
                        deadline, timeout, active))
                self._guard(deadline)
                responses.append(response)
                self._observe(index, response, codec.decode_step_ack_payload(response.payload))
                self._guard(deadline)
            self._failed = False
            return tuple(responses)
        finally:
            self._busy = False

    def close(self):
        """Close only the owned plant; caller cleans all borrowed resources."""
        if self._busy:
            raise RuntimeError("shared world callbacks must not reenter cleanup")
        self._failed = True
        self._coordinator.close()
