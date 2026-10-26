"""One staged ADC/IMU input owner over a borrowed canonical co-sim stream.

STEP acceptance and eventual BMI088 consumption are separate observations.
This endpoint never advances a CPU/world, retries a STEP, or closes its stream.
All waits borrow the component's absolute host deadline.
"""
from __future__ import annotations

from dataclasses import asdict
from collections.abc import Mapping
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
_MAX_ADC_CHANNELS = 16


def admit_adc(channel, payload):
    """Pure canonical ADC wire admission, shared by profile and execution."""
    if not isinstance(payload, Mapping) or set(payload) not in ({'raw'}, {'voltage_uv'}):
        raise ValueError('ADC input requires exactly raw or voltage_uv')
    key, value = next(iter(payload.items()))
    if type(value) is not int:
        raise TypeError('ADC input value must be an integer')
    if key == 'raw':
        kind = wire.STEP_SECTION_ADC_INPUT
        encoded = wire.encode_adc_input_v2(wire.AdcInputV2(channel, value))
    else:
        kind = wire.STEP_SECTION_ADC_VOLTAGE
        encoded = wire.encode_adc_voltage_v2(wire.AdcVoltageV2(channel, value))
    section = wire.StepSection(kind, 0, encoded)
    wire.validate_step_input_payload(wire.encode_step_payload((section,)))
    return section, {key: value}


class QemuCosimEndpoint:
    """Stage one cut, send once on commit; retain only IMU DONE obligations."""

    def __init__(self, stream: StepFramedSocket, *, context, check, imu=False, adc=False):
        if not isinstance(stream, StepFramedSocket):
            raise TypeError('QEMU co-sim endpoint requires a StepFramedSocket')
        if not callable(check):
            raise TypeError('QEMU co-sim endpoint requires a check callback')
        if type(imu) is not bool or type(adc) is not bool or not (imu or adc):
            raise ValueError('QEMU co-sim endpoint requires enabled ADC or IMU')
        self._imu_enabled, self._adc_enabled = imu, adc
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
        self._staged_cut = self._imu_input = None
        self._adc_inputs = {}
        self._reset_ack = None
        self._initial_seen = False
        self._started = self._failed = self._closed = False

    def _check(self):
        if self._closed:
            raise RuntimeError('QEMU co-sim endpoint is closed')
        if self._failed:
            raise RuntimeError('QEMU co-sim endpoint is frozen after failure')
        self._check_owner()
        if time.monotonic() >= self._ctx.deadline_monotonic:
            raise TimeoutError('QEMU co-sim endpoint host deadline expired')

    def _observe(self, cut_ns):
        check_timestamp(cut_ns)
        if cut_ns < self._observation_cut:
            raise ValueError('QEMU IMU observation cut regressed')
        self._observation_cut = cut_ns

    def _emit(self, kind, payload, cut_ns, mapped_time_ns=None, *, port='imu'):
        self._ctx.emit(port, kind, payload, source_time_ns=None,
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
            required = (wire.CAP_STEP | (wire.CAP_IMU if self._imu_enabled else 0)
                        | (wire.CAP_ADC if self._adc_enabled else 0))
            if (self._reset_ack is not None or self._started or payload.status != wire.STATUS_OK
                    or payload.virtual_time_ns != self._native_origin):
                raise RuntimeError('QEMU IMU RESET_ACK status or native origin mismatch')
            if payload.capabilities & required != required:
                raise BackendUnavailable('QEMU co-sim RESET_ACK lacks enabled STEP/IMU/ADC capabilities')
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
            observation = {key: value for key, value in record.items()
                           if key != 'acked' and not key.startswith('_')}
            observation.update(asdict(payload))
            if record['_has_imu']:
                self._emit(kind, observation, cut_ns, record['t_sim_ns'])
            if header.kind == wire.STEP_ACK:
                identity = {key: record[key] for key in
                            ('session_id', 'step_id', 't_sim_ns', 'dt_ns')}
                for channel, (_, value) in record['_adc'].items():
                    self._emit('adc-input-accepted',
                               {**identity, 'input': value, **asdict(payload)},
                               cut_ns, record['t_sim_ns'], port=f'adc1.ch{channel}')
            if header.kind == wire.STEP_DONE or not record['_has_imu']:
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
        self._emit(kind if self._imu_enabled else kind.replace('imu-', 'cosim-'),
                   {'header': asdict(header), 'response': asdict(decode(frame.payload))},
                   cut_ns, port='imu' if self._imu_enabled else None)

    def accept(self, frame):
        """Use canonical strict validation with this known input's DONE policy."""
        if frame.header.kind == wire.STEP_ACK:
            record = self._pending.get(frame.header.step_id)
            if record is None:
                raise RuntimeError('QEMU co-sim ACK has no matching input')
            self._rx.accept(frame, expect_step_done=record['_has_imu'])
        else:
            self._rx.accept(frame)

    def _pump(self, cut_ns):
        count = 0
        self._check()
        if self._stream.tx_pending:
            self._stream.flush()
        while count < _SERVICE_FRAMES:
            self._check()
            response = poll_v2_response(self._stream, self)
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
        """Stage a completed SI sample; initial state is observation-only."""
        try:
            self._check()
            if not self._started:
                raise RuntimeError('QEMU IMU endpoint requires start')
            if not self._imu_enabled:
                raise RuntimeError('QEMU IMU input is not enabled')
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
            self._stage_cut(cut_ns)
            if self._imu_input is not None:
                raise ValueError('duplicate IMU input at one cut')
            gyro, accel = imu_to_bmi088_units(envelope.sample)
            imu_bytes = wire.encode_imu_sample_v2(wire.ImuSampleV2(
                gyro, accel, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), cut_ns, 0))
            self._imu_input = source, imu_bytes
        except BaseException:
            self._failed = True
            raise

    def _stage_cut(self, cut_ns):
        if cut_ns <= self._last_cut:
            raise ValueError('QEMU co-sim input cut must increase strictly beyond session origin')
        if self._native_origin + cut_ns - self._origin > _SIGNED64_MAX:
            raise ValueError('mapped native co-sim time exceeds signed64 nanoseconds')
        if self._staged_cut is not None and self._staged_cut != cut_ns:
            raise RuntimeError('previous co-sim input cut has not been committed')
        self._staged_cut = cut_ns

    def receive_adc(self, channel, payload):
        """Stage one distinct ADC channel without transport side effects."""
        try:
            self._check()
            if not self._started or not self._adc_enabled:
                raise RuntimeError('QEMU ADC input requires enabled started endpoint')
            cut_ns = self._ctx.t_sim_ns
            self._ctx.check_io()
            self._observe(cut_ns)
            self._stage_cut(cut_ns)
            section, value = admit_adc(channel, payload)
            if channel in self._adc_inputs:
                raise ValueError('duplicate ADC channel at one cut')
            if len(self._adc_inputs) >= _MAX_ADC_CHANNELS:
                raise BufferError('QEMU ADC channel capacity per cut is 16')
            self._adc_inputs[channel] = section, value
        except BaseException:
            self._failed = True
            raise

    def commit_inputs(self, cut_ns):
        """Send the stabilized cut once; ACK admits bytes, DONE observes IMU reads."""
        try:
            self._check()
            if not self._started:
                raise RuntimeError('QEMU co-sim endpoint requires start')
            if cut_ns != self._ctx.t_sim_ns:
                raise ValueError('co-sim commit must match the current completed cut')
            self._observe(cut_ns)
            if self._staged_cut is None:
                return
            if cut_ns != self._staged_cut:
                raise ValueError('co-sim commit must match the staged cut')
            self._ctx.check_io()
            sections = []
            if self._imu_input is not None:
                sections.append(wire.StepSection(wire.STEP_SECTION_IMU_SAMPLE, 0,
                                                 self._imu_input[1]))
            sections.extend(value[0] for value in self._adc_inputs.values())
            payload = wire.encode_step_payload(sections)
            wire.validate_step_input_payload(payload)
            encoded = wire.build_step_frame(
                wire.STEP, self._step_id + 1, cut_ns, cut_ns - self._last_cut,
                session_id=self._tx.session_id, payload=payload)
            self._pump(cut_ns)
            if len(self._pending) >= _MAX_OUTSTANDING:
                raise BufferError('QEMU co-sim outstanding input capacity is 64')
            self._step_id += 1
            record = {'session_id': self._tx.session_id, 'step_id': self._step_id,
                      't_sim_ns': cut_ns, 'dt_ns': cut_ns - self._last_cut,
                      '_has_imu': self._imu_input is not None,
                      '_adc': self._adc_inputs, 'acked': False}
            if self._imu_input is not None:
                record.update(input=self._imu_input[0],
                              wire=asdict(wire.decode_imu_sample_v2(self._imu_input[1])))
            self._pending[self._step_id] = record
            self._send(encoded)
            self._wait(lambda: record['acked'], cut_ns)
            self._last_cut = cut_ns
            self._staged_cut = self._imu_input = None
            self._adc_inputs = {}
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
