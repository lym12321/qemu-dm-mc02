#![cfg(all(feature = "simulation", feature = "cosim-wire", target_os = "linux"))]
use sim_host::{
    cosim_session::{CosimSession, Response, SessionOptions, CAP_IMU, CAP_STEP},
    step_wire::{Frame, ImuSample, Kind, ResetAck, StepAck, StepDone, StepInput},
    transports::stream::StreamTransport,
};
use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    sync::Arc,
};
const ID: u32 = 123;
fn record(kind: Kind, id: u64, time: u64, dt: u64, payload: Vec<u8>) -> Frame {
    Frame {
        kind,
        step_id: id,
        time_ns: time,
        dt_ns: dt,
        session_id: ID,
        payload,
    }
}
fn write(peer: &mut UnixStream, frame: &Frame) {
    let body = frame.encode_body().unwrap();
    peer.write_all(&(body.len() as u32).to_le_bytes()).unwrap();
    peer.write_all(&body).unwrap();
}
fn read(peer: &mut UnixStream) -> Frame {
    let mut size = [0; 4];
    peer.read_exact(&mut size).unwrap();
    let mut bytes = vec![0; u32::from_le_bytes(size) as usize];
    peer.read_exact(&mut bytes).unwrap();
    Frame::decode_body(&bytes).unwrap()
}
fn started(require_done: bool) -> (CosimSession, StreamTransport<UnixStream>, UnixStream) {
    let (host, mut peer) = UnixStream::pair().unwrap();
    host.set_nonblocking(true).unwrap();
    peer.set_read_timeout(Some(std::time::Duration::from_secs(1)))
        .unwrap();
    let mut stream = StreamTransport::new(host, 4).unwrap();
    let mut session = CosimSession::new_worker(
        SessionOptions {
            session_id: ID,
            sim_origin_ns: 4000,
            native_origin_ns: 0,
            required_capabilities: CAP_STEP | CAP_IMU,
        },
        require_done,
    )
    .unwrap();
    session.begin_reset(&mut stream).unwrap();
    assert_eq!(read(&mut peer).kind, Kind::Reset);
    write(
        &mut peer,
        &record(
            Kind::ResetAck,
            0,
            4000,
            0,
            ResetAck {
                status: 0,
                capabilities: 3,
                virtual_time_ns: 900,
                queue_depth: 0,
            }
            .encode()
            .unwrap(),
        ),
    );
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert_eq!(session.native_origin_ns(), 900);
    (session, stream, peer)
}
fn input(time: u64) -> StepInput {
    StepInput::CompactImu(ImuSample {
        gyro_dps: [0.0; 3],
        accel_g: [0.0, 0.0, 1.0],
        gyro_bias_dps: [0.0; 3],
        accel_bias_g: [0.0; 3],
        sensor_time_ns: time,
        sample_id: 1,
    })
}
fn ack(status: u32) -> Frame {
    record(
        Kind::StepAck,
        1,
        5000,
        1000,
        StepAck {
            status,
            queue_depth: 1,
            dropped_count: 0,
        }
        .encode()
        .unwrap(),
    )
}
fn done() -> Frame {
    record(
        Kind::StepDone,
        1,
        5000,
        1000,
        StepDone {
            status: 0,
            consumed_mask: 3,
            missing_mask: 0,
            queue_depth: 0,
            dropped_count: 0,
        }
        .encode()
        .unwrap(),
    )
}
#[test]
fn provisional_queue_full_exact_replay_and_optional_late_done_keep_one_submission() {
    let (mut session, mut stream, mut peer) = started(false);
    session.submit(&mut stream, 5000, input(5000)).unwrap();
    let first = read(&mut peer);
    write(&mut peer, &ack(1));
    session
        .pump(&mut stream, |event| {
            assert!(matches!(
                event.response,
                Response::StepAck(StepAck { status: 1, .. })
            ));
            Ok(())
        })
        .unwrap();
    assert!(!session.acknowledged(1));
    session.retry_last(&mut stream).unwrap();
    assert_eq!(read(&mut peer), first);
    assert_eq!(session.last_submission(), (1, 5000));
    write(&mut peer, &ack(0));
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert_eq!(session.outstanding(), 0);
    write(&mut peer, &done());
    session.pump(&mut stream, |_| Ok(())).unwrap();
    session.retry_last(&mut stream).unwrap();
    assert_eq!(read(&mut peer), first);
    write(&mut peer, &ack(0));
    write(&mut peer, &done());
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert!(session.failure().is_none());
}
#[test]
fn strict_worker_done_obligation_allows_only_ack_paired_done_replay() {
    let (mut session, mut stream, mut peer) = started(true);
    session.submit(&mut stream, 5000, input(5000)).unwrap();
    read(&mut peer);
    write(&mut peer, &ack(0));
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert_eq!(session.outstanding(), 1);
    write(&mut peer, &done());
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert_eq!(session.outstanding(), 0);
    write(&mut peer, &done());
    let failure = session.pump(&mut stream, |_| Ok(())).unwrap_err();
    assert!(Arc::ptr_eq(&failure, session.failure().unwrap()));
}
#[test]
fn unsolicited_reset_cancels_old_retry_before_matching_ack_and_retains_following_suffix() {
    let (mut session, mut stream, mut peer) = started(true);
    session.submit(&mut stream, 5000, input(5000)).unwrap();
    read(&mut peer);
    let mut reset = record(Kind::Reset, 0, 100, 0, vec![]);
    reset.session_id = 456;
    write(&mut peer, &reset);
    session
        .pump(&mut stream, |event| {
            assert_eq!(event.response, Response::Reset);
            Ok(())
        })
        .unwrap();
    assert_eq!(session.outstanding(), 0);
    assert!(!session.started());
    assert!(session.retry_last(&mut stream).is_err());
    let mut ack = record(
        Kind::ResetAck,
        0,
        100,
        0,
        ResetAck {
            status: 0,
            capabilities: 3,
            virtual_time_ns: 100,
            queue_depth: 0,
        }
        .encode()
        .unwrap(),
    );
    ack.session_id = 456;
    write(&mut peer, &ack);
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert!(session.started());
    assert_eq!(
        (session.session_id(), session.reset_origin_ns()),
        (456, 100)
    );
    session.submit(&mut stream, 1100, input(1100)).unwrap();
    let frame = read(&mut peer);
    assert_eq!(
        (frame.session_id, frame.step_id, frame.time_ns, frame.dt_ns),
        (456, 1, 1100, 1000)
    );
}

#[test]
fn reset_cancels_logical_retry_without_deleting_partial_physical_step_suffix() {
    use std::{
        cell::{Cell, RefCell},
        collections::VecDeque,
        io,
        rc::Rc,
    };
    struct Partial {
        input: Rc<RefCell<VecDeque<u8>>>,
        output: Rc<RefCell<Vec<u8>>>,
        blocked: Rc<Cell<bool>>,
        partial: Rc<Cell<bool>>,
    }
    impl Read for Partial {
        fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
            let mut input = self.input.borrow_mut();
            if input.is_empty() {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            let count = bytes.len().min(input.len());
            for b in &mut bytes[..count] {
                *b = input.pop_front().unwrap();
            }
            Ok(count)
        }
    }
    impl Write for Partial {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.blocked.get() {
                return Err(io::ErrorKind::WouldBlock.into());
            }
            let count = if self.partial.replace(false) {
                self.blocked.set(true);
                7.min(bytes.len())
            } else {
                bytes.len()
            };
            self.output.borrow_mut().extend(&bytes[..count]);
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let (input, output, blocked, partial) = (
        Rc::new(RefCell::new(VecDeque::new())),
        Rc::new(RefCell::new(vec![])),
        Rc::new(Cell::new(false)),
        Rc::new(Cell::new(false)),
    );
    let mut stream = StreamTransport::new(
        Partial {
            input: input.clone(),
            output: output.clone(),
            blocked: blocked.clone(),
            partial: partial.clone(),
        },
        4,
    )
    .unwrap();
    let mut session = CosimSession::new_worker(
        SessionOptions {
            session_id: ID,
            sim_origin_ns: 0,
            native_origin_ns: 0,
            required_capabilities: 3,
        },
        true,
    )
    .unwrap();
    session.begin_reset(&mut stream).unwrap();
    let append = |frame: Frame| {
        let body = frame.encode_body().unwrap();
        input.borrow_mut().extend((body.len() as u32).to_le_bytes());
        input.borrow_mut().extend(body);
    };
    append(record(
        Kind::ResetAck,
        0,
        0,
        0,
        ResetAck {
            status: 0,
            capabilities: 3,
            virtual_time_ns: 0,
            queue_depth: 0,
        }
        .encode()
        .unwrap(),
    ));
    session.pump(&mut stream, |_| Ok(())).unwrap();
    output.borrow_mut().clear();
    partial.set(true);
    session.submit(&mut stream, 1000, input_sample()).unwrap();
    assert_eq!(output.borrow().len(), 7);
    assert!(stream.tx_pending());
    assert!(!session.retry_last(&mut stream).unwrap());
    assert_eq!(stream.pending_records(), 1);
    let mut reset = record(Kind::Reset, 0, 10, 0, vec![]);
    reset.session_id = 789;
    append(reset);
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert!(session.retry_last(&mut stream).is_err());
    assert_eq!(stream.pending_records(), 1);
    blocked.set(false);
    assert!(stream.flush().unwrap());
    let bytes = output.borrow();
    let body = Frame::decode_body(&bytes[4..]).unwrap();
    assert_eq!(
        (body.kind, body.step_id, body.session_id),
        (Kind::Step, 1, ID)
    );
    assert_eq!(
        bytes.len(),
        4 + u32::from_le_bytes(bytes[..4].try_into().unwrap()) as usize
    );
    fn input_sample() -> StepInput {
        StepInput::CompactImu(ImuSample {
            gyro_dps: [0.0; 3],
            accel_g: [0.0, 0.0, 1.0],
            gyro_bias_dps: [0.0; 3],
            accel_bias_g: [0.0; 3],
            sensor_time_ns: 1000,
            sample_id: 1,
        })
    }
}
