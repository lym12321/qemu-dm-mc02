"""Thin typed IMU receiver over a borrowed canonical co-sim stream.

STEP acceptance and eventual BMI088 consumption are separate observations.
This endpoint never advances a CPU/world, retries a STEP, or closes its stream.
All waits borrow the component's absolute host deadline.
"""
from __future__ import annotations

from dataclasses import asdict
import select
import time

from sim.api import BackendUnavailable, ImuSignalSample
from sim.api.events import check_timestamp
from sim.devices.bmi088 import imu_to_bmi088_units
from sim.transports import step_codec as wire
from sim.transports.cosim import StepFramedSocket, poll_v2_response


_MAX_OUTSTANDING = 64
_SERVICE_FRAMES = 64
_SIGNED64_MAX = 0x7fffffffffffffff


class QemuImuEndpoint:
    """Receive completed SI cuts; retain bounded ACK/DONE identity metadata."""

    def __init__(self, stream: StepFramedSocket, *, context, check):
        if not isinstance(stream, StepFramedSocket):
            raise TypeError('QEMU IMU endpoint requires a StepFramedSocket')
        if not callable(check):
            raise TypeError('QEMU IMU endpoint requires a check callback')
        self._stream, self._ctx, self._check_owner = stream, context, check
        self._tx = wire.StepSessionValidator.host_to_qemu(generate_session_id=True)
        self._rx = wire.StepSessionValidator.qemu_to_host(
            session_id=self._tx.session_id, require_step_done=True)
        self._origin = context.session.t_sim_ns
        check_timestamp(self._origin)
        self._last_cut = self._observation_cut = self._origin
        self._native_origin = None
        self._step_id = 0
        self._pending = {}
        self._reset_ack = None
        self._initial_seen = False
        self._started = self._failed = self._closed = False

    def _check(self):
        if self._closed:
            raise RuntimeError('QEMU IMU endpoint is closed')
        if self._failed:
            raise RuntimeError('QEMU IMU endpoint is frozen after failure')
        self._check_owner()
        if time.monotonic() >= self._ctx.deadline_monotonic:
            raise TimeoutError('QEMU IMU endpoint host deadline expired')

    def _observe(self, cut_ns):
        check_timestamp(cut_ns)
        if cut_ns < self._observation_cut:
            raise ValueError('QEMU IMU observation cut regressed')
        self._observation_cut = cut_ns

    def _emit(self, kind, payload, cut_ns, mapped_time_ns=None):
        self._ctx.emit('imu', kind, payload, source_time_ns=None,
                       mapped_time_ns=cut_ns if mapped_time_ns is None else mapped_time_ns,
                       delivery_time_ns=cut_ns, quality='sampled')

    def _send(self, encoded):
        self._check()
        self._tx.accept(wire.decode_step_frame(encoded))
        self._stream.send(encoded)
        self._check()

    def start(self, native_origin_ns):
        """Send one RESET and bind the already-admitted native clock origin."""
        try:
            self._check()
            if self._started or self._native_origin is not None:
                raise RuntimeError('QEMU IMU endpoint can only start once')
            if (type(native_origin_ns) is not int or
                    not 0 <= native_origin_ns <= _SIGNED64_MAX):
                raise ValueError('native IMU origin must be signed64 nonnegative nanoseconds')
            self._native_origin = native_origin_ns
            self._rx.begin_reset(0, self._origin, self._tx.session_id)
            self._send(wire.build_step_frame(
                wire.RESET, 0, self._origin, session_id=self._tx.session_id))
            self._wait(lambda: self._reset_ack is not None, self._origin)
            self._started = True
        except BaseException:
            self._failed = True
            raise

    def _response(self, frame, payload, cut_ns):
        header = frame.header
        if header.kind == wire.RESET:
            raise RuntimeError('unexpected QEMU IMU session RESET')
        if header.kind == wire.RESET_ACK:
            required = wire.CAP_STEP | wire.CAP_IMU
            if (self._reset_ack is not None or self._started or payload.status != wire.STATUS_OK
                    or payload.virtual_time_ns != self._native_origin):
                raise RuntimeError('QEMU IMU RESET_ACK status or native origin mismatch')
            if payload.capabilities & required != required:
                raise BackendUnavailable('QEMU IMU RESET_ACK lacks STEP/IMU capabilities')
            self._reset_ack = payload
            return
        if header.kind in (wire.STEP_ACK, wire.STEP_DONE):
            record = self._pending.get(header.step_id)
            if (record is None or header.t_sim_ns != record['t_sim_ns']
                    or header.dt_ns != record['dt_ns']):
                raise RuntimeError('QEMU IMU response has no matching input')
            if header.kind == wire.STEP_ACK:
                if record['acked']:
                    raise RuntimeError('unexpected duplicate QEMU IMU STEP_ACK')
                if payload.status != wire.STATUS_OK:
                    raise RuntimeError(f'QEMU IMU STEP_ACK failed: status={payload.status}')
                record['acked'] = True
                kind = 'imu-input-accepted'
            else:
                if not record['acked']:
                    raise RuntimeError('QEMU IMU STEP_DONE arrived before acceptance')
                kind = 'imu-consumption'
            observation = {key: value for key, value in record.items() if key != 'acked'}
            observation.update(asdict(payload))
            self._emit(kind, observation, cut_ns, record['t_sim_ns'])
            if header.kind == wire.STEP_DONE:
                del self._pending[header.step_id]
            return
        decoders = {
            wire.TELEMETRY: ('imu-telemetry', wire.decode_board_telemetry_payload),
            wire.DIAGNOSTICS: ('imu-diagnostics', wire.decode_diagnostics_payload),
            wire.MOTOR_STATE: ('imu-motor-state', wire.decode_motor_state),
        }
        kind, decode = decoders[header.kind]
        # Auxiliary response clocks are independent of input cuts. Preserve
        # their header verbatim, without treating it as a native source time.
        self._emit(kind, {'header': asdict(header), 'response': asdict(decode(frame.payload))},
                   cut_ns)

    def _pump(self, cut_ns):
        count = 0
        self._check()
        if self._stream.tx_pending:
            self._stream.flush()
        while count < _SERVICE_FRAMES:
            self._check()
            response = poll_v2_response(self._stream, self._rx)
            if response is None:
                break
            self._response(*response, cut_ns)
            count += 1
        self._check()
        return count

    def _wait(self, ready, cut_ns):
        while True:
            count = self._pump(cut_ns)
            if ready():
                return
            if count == _SERVICE_FRAMES:
                continue
            remaining = self._ctx.deadline_monotonic - time.monotonic()
            self._check()
            readable, writable, _ = select.select(
                [self._stream.sock], [self._stream.sock] if self._stream.tx_pending else [],
                [], remaining)
            self._check()
            if not readable and not writable:
                raise TimeoutError('QEMU IMU response host deadline expired')

    def receive_imu(self, envelope: ImuSignalSample):
        """Send one positive completed cut and wait only for its admission ACK."""
        try:
            self._check()
            if not self._started:
                raise RuntimeError('QEMU IMU endpoint requires start')
            if not isinstance(envelope, ImuSignalSample):
                raise TypeError('QEMU IMU input requires an ImuSignalSample')
            cut_ns = envelope.mapped_time_ns
            if cut_ns != self._ctx.t_sim_ns:
                raise ValueError('QEMU IMU input must match the current completed cut')
            self._ctx.check_io()
            self._observe(cut_ns)
            source = {'gyro_rad_s': envelope.sample.gyro_rad_s,
                      'accel_m_s2': envelope.sample.accel_m_s2,
                      'source_time_ns': envelope.source_time_ns,
                      'mapped_time_ns': cut_ns}
            if cut_ns == self._origin and not self._initial_seen and self._step_id == 0:
                self._initial_seen = True
                self._emit('imu-input-initial', {'input': source}, cut_ns)
                return
            if cut_ns <= self._last_cut:
                raise ValueError('QEMU IMU input cut must increase strictly')
            if self._native_origin + cut_ns - self._origin > _SIGNED64_MAX:
                raise ValueError('mapped native IMU time exceeds signed64 nanoseconds')
            gyro, accel = imu_to_bmi088_units(envelope.sample)
            imu_bytes = wire.encode_imu_sample_v2(wire.ImuSampleV2(
                gyro, accel, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), cut_ns, 0))
            imu = wire.decode_imu_sample_v2(imu_bytes)
            encoded = wire.build_step_frame(
                wire.STEP, self._step_id + 1, cut_ns, cut_ns - self._last_cut,
                session_id=self._tx.session_id,
                payload=wire.encode_step_payload((wire.StepSection(
                    wire.STEP_SECTION_IMU_SAMPLE, 0, imu_bytes),)))
            self._pump(cut_ns)
            if len(self._pending) >= _MAX_OUTSTANDING:
                raise BufferError('QEMU IMU outstanding input capacity is 64')
            self._step_id += 1
            record = {'session_id': self._tx.session_id, 'step_id': self._step_id,
                      't_sim_ns': cut_ns, 'dt_ns': cut_ns - self._last_cut,
                      'input': source, 'wire': asdict(imu), 'acked': False}
            self._pending[self._step_id] = record
            self._send(encoded)
            self._wait(lambda: record['acked'], cut_ns)
            self._last_cut = cut_ns
        except BaseException:
            self._failed = True
            raise

    def service(self, cut_ns):
        """Observe at most 64 available responses without waiting for DONE."""
        try:
            self._check()
            if not self._started:
                raise RuntimeError('QEMU IMU endpoint requires start')
            self._observe(cut_ns)
            self._pump(cut_ns)
        except BaseException:
            self._failed = True
            raise

    def close(self):
        """Cancel logical use without flushing, reading or closing borrowed IO."""
        self._closed = True
