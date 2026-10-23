"""Typed IMU input over real socketpair framing, without CPU/world ownership."""
from contextlib import contextmanager
import math
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from sim.api import BackendUnavailable, Event, ImuSample, ImuSignalSample
from sim.machines.qemu.imu import QemuImuEndpoint
from sim.transports import step_codec as wire
from sim.transports.cosim import StepFramedSocket


class Peer:
    def __init__(self, sock, respond):
        self.sock, self.respond = sock, respond
        self.frames, self.errors = [], []
        self.stopping = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def read(self, count):
        data = b''
        while len(data) < count:
            part = self.sock.recv(count - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    def run(self):
        try:
            while True:
                prefix = self.read(4)
                frame = wire.decode_step_frame(prefix + self.read(int.from_bytes(prefix, 'little')))
                self.frames.append(frame)
                self.respond(self, frame)
        except (EOFError, OSError) as exc:
            if not self.stopping:
                self.errors.append(exc)
        except BaseException as exc:
            self.errors.append(exc)

    def send(self, frame, kind, payload, *, t_sim_ns=None, step_id=None, dt_ns=None):
        header = frame.header
        self.sock.sendall(wire.build_step_frame(
            kind, header.step_id if step_id is None else step_id,
            header.t_sim_ns if t_sim_ns is None else t_sim_ns,
            header.dt_ns if dt_ns is None else dt_ns,
            session_id=header.session_id, payload=payload))

    def reset_ack(self, frame, *, status=0, capabilities=3, native=0):
        self.send(frame, wire.RESET_ACK, wire.encode_reset_ack_payload(
            wire.ResetAckPayload(status, capabilities, native, 0)))

    def ack(self, frame, *, status=0):
        self.send(frame, wire.STEP_ACK, wire.encode_step_ack_payload(
            wire.StepAckPayload(status, 1, 0)))

    def done(self, frame, *, status=0, consumed=3, missing=0):
        self.send(frame, wire.STEP_DONE, wire.encode_step_done_payload(
            wire.StepDonePayload(status, consumed, missing, 0, 0)))


class Context:
    def __init__(self, origin):
        self.session = SimpleNamespace(t_sim_ns=origin)
        self.t_sim_ns = origin
        self.deadline_monotonic = time.monotonic() + 2
        self.events = []
        self.io_checks = 0

    def check_io(self):
        self.io_checks += 1

    def emit(self, port, kind, payload, **times):
        event = Event(1, len(self.events), 'board', port, kind, payload, **times)
        self.events.append(event)
        return event


@contextmanager
def endpoint(respond=None, *, origin=0, native=0, check=None, socket_wrapper=None):
    left, right = socket.socketpair()
    left.setblocking(False)
    context = Context(origin)

    def default(peer, frame):
        if frame.header.kind == wire.RESET:
            peer.reset_ack(frame, native=native)
        else:
            peer.ack(frame)

    peer = Peer(right, respond or default)
    stream = StepFramedSocket(left if socket_wrapper is None else socket_wrapper(left))
    receiver = QemuImuEndpoint(stream, context=context, check=check or (lambda: None))
    try:
        yield receiver, peer, context, stream
    finally:
        receiver.close()
        peer.stopping = True
        left.close()
        right.shutdown(socket.SHUT_RDWR)
        right.close()
        peer.thread.join(timeout=2)
        assert not peer.thread.is_alive()
        assert not peer.errors


def sample(cut, *, source=None, gyro=(0.0, 0.0, 0.0), accel=(0.0, 0.0, 9.81)):
    return ImuSignalSample(ImuSample(gyro, accel), source, cut)


def deliver(receiver, context, cut, **kwargs):
    context.t_sim_ns = cut
    receiver.receive_imu(sample(cut, **kwargs))


def events(context, kind):
    return [event for event in context.events if event.kind == kind]


def test_zero_cut_is_observation_only_then_signed_axes_have_literal_wire_units():
    with endpoint() as (receiver, peer, context, _):
        receiver.start(0)
        deliver(receiver, context, 0)
        assert len(peer.frames) == 1 and peer.frames[0].header.kind == wire.RESET
        assert peer.frames[0].header.session_id != 0
        deliver(receiver, context, 10_000_000, source=123,
                gyro=(math.pi / 2, -math.pi, 0.0), accel=(9.80665, -19.6133, 0.0))
        frame = peer.frames[1]
        assert (frame.header.step_id, frame.header.t_sim_ns, frame.header.dt_ns) == (1, 10_000_000, 10_000_000)
        sections = wire.decode_step_payload(frame.payload)
        # Independent little-endian literals: 90,-180,0 degrees/s; 1,-2,0 g;
        # zero biases; sensor input cut; no host-assigned source identity.
        expected = bytes.fromhex(
            '0000b442000034c3000000000000803f000000c000000000'
            '000000000000000000000000000000000000000000000000')
        expected += struct.pack('<QI', 10_000_000, 0)
        assert sections[0].payload == expected
        accepted = events(context, 'imu-input-accepted')[0]
        assert accepted.payload['input']['source_time_ns'] == 123
        assert accepted.payload['wire']['sensor_time_ns'] == 10_000_000
        assert accepted.source_time_ns is None
        assert accepted.mapped_time_ns == accepted.delivery_time_ns == 10_000_000
        assert accepted.quality == 'sampled'
        assert context.io_checks == 2


def test_nonzero_epochs_offgrid_cuts_and_float32_metadata_are_independent():
    with endpoint(origin=900, native=1234) as (receiver, peer, context, _):
        receiver.start(1234)
        deliver(receiver, context, 900)
        deliver(receiver, context, 903, source=7)
        deliver(receiver, context, 910, source=9)
        assert [(f.header.step_id, f.header.t_sim_ns, f.header.dt_ns) for f in peer.frames] == [
            (0, 900, 0), (1, 903, 3), (2, 910, 7)]
        metadata = events(context, 'imu-input-accepted')[0].payload
        # float32 round trip is evidence about the transmitted value, distinct
        # from the source's float64 proper acceleration and from native time.
        assert metadata['wire']['accel_g'][2] == 1.0003416538238525
        assert metadata['input']['accel_m_s2'][2] == 9.81
        assert metadata['wire']['sensor_time_ns'] == 903


@pytest.mark.parametrize('status, capabilities, native, error', [
    (2, 3, 0, RuntimeError), (0, 1, 0, BackendUnavailable),
    (0, 2, 0, BackendUnavailable), (0, 3, 1, RuntimeError)])
def test_reset_ack_status_capabilities_and_native_origin_reject_and_freeze(status, capabilities, native, error):
    def respond(peer, frame):
        peer.reset_ack(frame, status=status, capabilities=capabilities, native=native)

    with endpoint(respond) as (receiver, peer, _, _):
        with pytest.raises(error, match='RESET_ACK') as failure:
            receiver.start(0)
        assert type(failure.value) is error
        with pytest.raises(RuntimeError, match='frozen'):
            receiver.start(0)
        assert len(peer.frames) == 1


def test_older_done_is_retained_while_waiting_for_new_ack_and_missing_is_advisory():
    def respond(peer, frame):
        if frame.header.kind == wire.RESET:
            peer.reset_ack(frame)
        else:
            if frame.header.step_id == 2:
                peer.done(peer.frames[1], status=2, consumed=1, missing=2)
            peer.ack(frame)
            if frame.header.step_id == 2:
                peer.done(frame)

    with endpoint(respond) as (receiver, _, context, _):
        receiver.start(0)
        deliver(receiver, context, 10)
        deliver(receiver, context, 17)
        receiver.service(20)
        done = events(context, 'imu-consumption')
        assert [(e.payload['step_id'], e.payload['status'], e.payload['consumed_mask'],
                 e.payload['missing_mask']) for e in done] == [(1, 2, 1, 2), (2, 0, 3, 0)]
        assert done[0].mapped_time_ns == 10 and done[0].delivery_time_ns == 17
        assert done[0].source_time_ns is None


def test_capacity_is_atomic_at_64_inputs_without_done_and_never_replays():
    with endpoint() as (receiver, peer, context, _):
        receiver.start(0)
        for cut in range(1, 65):
            deliver(receiver, context, cut)
        with pytest.raises(BufferError, match='capacity is 64'):
            deliver(receiver, context, 65)
        with pytest.raises(RuntimeError, match='frozen'):
            deliver(receiver, context, 66)
        assert len(peer.frames) == 65
        assert len(events(context, 'imu-input-accepted')) == 64


@pytest.mark.parametrize('mode', ['queue-full', 'unexpected-reset', 'ack-time', 'ack-dt', 'malformed', 'disconnect'])
def test_terminal_protocol_and_io_errors_freeze_without_retry(mode):
    def respond(peer, frame):
        if frame.header.kind == wire.RESET:
            peer.reset_ack(frame)
        elif mode == 'queue-full':
            peer.ack(frame, status=1)
        elif mode == 'unexpected-reset':
            peer.send(frame, wire.RESET, b'', dt_ns=0)
        elif mode in ('ack-time', 'ack-dt'):
            peer.send(frame, wire.STEP_ACK,
                      wire.encode_step_ack_payload(wire.StepAckPayload(0, 1, 0)),
                      t_sim_ns=11 if mode == 'ack-time' else 10,
                      dt_ns=9 if mode == 'ack-dt' else 10)
        elif mode == 'malformed':
            peer.send(frame, wire.STEP_ACK, bytes.fromhex('00000000010000000000000001000000'))
        else:
            peer.sock.shutdown(socket.SHUT_WR)

    with endpoint(respond) as (receiver, peer, context, _):
        receiver.start(0)
        with pytest.raises((RuntimeError, ValueError, ConnectionError)):
            deliver(receiver, context, 10)
        with pytest.raises(RuntimeError, match='frozen'):
            receiver.service(10)
        assert len(peer.frames) == 2
        assert not events(context, 'imu-input-accepted')


def test_shared_deadline_bounds_silent_ack_without_retry():
    def respond(peer, frame):
        if frame.header.kind == wire.RESET:
            peer.reset_ack(frame)

    with endpoint(respond) as (receiver, peer, context, _):
        receiver.start(0)
        context.deadline_monotonic = time.monotonic() + .03
        with pytest.raises(TimeoutError, match='deadline'):
            deliver(receiver, context, 10)
        assert len(peer.frames) == 2
        with pytest.raises(RuntimeError, match='frozen'):
            deliver(receiver, context, 20)


@pytest.mark.parametrize('mode', ['offcut', 'duplicate', 'native-overflow'])
def test_bad_cut_rejects_before_new_wire_bytes(mode):
    native = 0x7fffffffffffffff if mode == 'native-overflow' else 0
    with endpoint(native=native) as (receiver, peer, context, _):
        receiver.start(native)
        if mode == 'duplicate':
            deliver(receiver, context, 0)
        with pytest.raises(ValueError):
            if mode == 'offcut':
                receiver.receive_imu(sample(1))
            else:
                deliver(receiver, context, 0 if mode == 'duplicate' else 1)
        assert len(peer.frames) == 1


def test_auxiliary_telemetry_retains_independent_header_clock():
    def respond(peer, frame):
        if frame.header.kind == wire.RESET:
            peer.reset_ack(frame, native=7)
        else:
            peer.send(frame, wire.TELEMETRY,
                      wire.encode_board_telemetry_payload(wire.BoardTelemetryPayload(1, 2, 3, 4, 5, 6)),
                      step_id=500, t_sim_ns=999_999, dt_ns=0)
            peer.ack(frame)

    with endpoint(respond, origin=100, native=7) as (receiver, _, context, _):
        # This fake native reset origin intentionally differs from telemetry.
        receiver.start(7)
        deliver(receiver, context, 110)
        event = events(context, 'imu-telemetry')[0]
        assert event.payload['header']['t_sim_ns'] == 999_999
        assert event.payload['response']['led_rgb'] == 1
        assert event.source_time_ns is None and event.mapped_time_ns == 110


def test_service_is_bounded_and_close_never_flushes_or_closes_borrow(monkeypatch):
    with endpoint() as (receiver, peer, context, stream):
        receiver.start(0)
        deliver(receiver, context, 1)
        # Queue an exact 65-frame decoded suffix using the canonical framing
        # API; no duplicate or alternate telemetry clock is invented.
        stream.push_front(wire.decode_step_frame(wire.build_step_frame(
            wire.TELEMETRY, cut, cut, session_id=peer.frames[0].header.session_id,
            payload=wire.encode_board_telemetry_payload(wire.BoardTelemetryPayload(0, 0, 0, 0, 0, 0))))
                          for cut in range(1, 66))
        receiver.service(2)
        assert len(events(context, 'imu-telemetry')) == 64
        assert len(stream.pending_frames) == 1
        stream.tx_queue.append(memoryview(b'physical suffix'))
        monkeypatch.setattr(stream, 'flush', lambda: pytest.fail('close flushed borrow'))
        receiver.close()
        assert bytes(stream.tx_queue[0]) == b'physical suffix'
        assert len(stream.pending_frames) == 1
        assert stream.sock.fileno() >= 0
        with pytest.raises(RuntimeError, match='closed'):
            receiver.service(2)


def test_partial_transmit_flushes_exact_suffix_without_second_step():
    class PartialSocket:
        def __init__(self, sock):
            self.sock, self.partial = sock, True

        def send(self, data):
            if self.partial:
                self.partial = False
                return self.sock.send(data[:19])
            return self.sock.send(data)

        def recv(self, size):
            return self.sock.recv(size)

        def fileno(self):
            return self.sock.fileno()

    with endpoint(socket_wrapper=PartialSocket) as (receiver, peer, context, stream):
        receiver.start(0)
        stream.sock.partial = True
        deliver(receiver, context, 10)
        assert len(peer.frames) == 2
        assert [f.header.kind for f in peer.frames] == [wire.RESET, wire.STEP]
        assert not stream.tx_pending


def test_partial_received_ack_survives_until_actual_suffix():
    prefix_sent = threading.Event()
    release_suffix = threading.Event()

    def respond(peer, frame):
        if frame.header.kind == wire.RESET:
            peer.reset_ack(frame)
        else:
            encoded = wire.build_step_frame(
                wire.STEP_ACK, frame.header.step_id, frame.header.t_sim_ns, frame.header.dt_ns,
                session_id=frame.header.session_id,
                payload=wire.encode_step_ack_payload(wire.StepAckPayload(0, 1, 0)))
            peer.sock.sendall(encoded[:19])
            prefix_sent.set()
            assert release_suffix.wait(1)
            peer.sock.sendall(encoded[19:])

    with endpoint(respond) as (receiver, peer, context, stream):
        receiver.start(0)
        context.t_sim_ns = 10
        outcome = []

        def deliver_in_thread():
            try:
                receiver.receive_imu(sample(10))
                outcome.append('accepted')
            except BaseException as exc:
                outcome.append(exc)

        delivery = threading.Thread(target=deliver_in_thread)
        delivery.start()
        assert prefix_sent.wait(1)
        assert not events(context, 'imu-input-accepted')
        release_suffix.set()
        delivery.join(1)
        assert outcome == ['accepted']
        assert len(peer.frames) == 2 and not stream.buffer


def test_owner_failure_after_partial_send_retains_physical_suffix(monkeypatch):
    class InterruptedSocket:
        def __init__(self, sock):
            self.sock, self.interrupt = sock, False
            self.interrupted = False
            self.encoded = None

        def send(self, data):
            if self.interrupt:
                self.encoded = bytes(data)
                self.interrupted = True
                return self.sock.send(data[:19])
            return self.sock.send(data)

        def recv(self, count):
            return self.sock.recv(count)

        def fileno(self):
            return self.sock.fileno()

    connection = None

    def check():
        if connection is not None and connection.interrupted:
            raise RuntimeError('owner process exited')

    with endpoint(check=check, socket_wrapper=InterruptedSocket) as (receiver, _, context, stream):
        receiver.start(0)
        connection = stream.sock
        connection.interrupt = True
        with pytest.raises(RuntimeError, match='owner process exited'):
            deliver(receiver, context, 10)
        assert len(stream.tx_queue) == 1
        assert bytes(stream.tx_queue[0]) == connection.encoded[19:]
        with pytest.raises(RuntimeError, match='frozen'):
            receiver.service(10)
        monkeypatch.setattr(stream, 'flush', lambda: pytest.fail('failure cleanup flushed suffix'))
        receiver.close()
        assert bytes(stream.tx_queue[0]) == connection.encoded[19:]


def test_reset_wait_uses_shared_deadline_without_repeat_reset():
    with endpoint(lambda _peer, _frame: None) as (receiver, peer, context, _):
        context.deadline_monotonic = time.monotonic() + .03
        with pytest.raises(TimeoutError, match='deadline'):
            receiver.start(0)
        assert len(peer.frames) == 1
        with pytest.raises(RuntimeError, match='frozen'):
            receiver.start(0)


def test_finite_si_outside_wire_float32_range_rejects_before_step_bytes():
    with endpoint() as (receiver, peer, context, _):
        receiver.start(0)
        context.t_sim_ns = 10
        with pytest.raises((OverflowError, ValueError)):
            receiver.receive_imu(sample(10, gyro=(1e100, 0.0, 0.0)))
        assert len(peer.frames) == 1
        with pytest.raises(RuntimeError, match='frozen'):
            receiver.service(10)
