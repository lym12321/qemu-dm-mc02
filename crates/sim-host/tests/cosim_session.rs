#![cfg(all(target_os = "linux", feature = "machine"))]
use sim_host::{machine::cosim_session::*, step_wire::*, transports::stream::StreamTransport};
use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    sync::Arc,
};
const ID: u32 = 0x1234_abcd;
fn session() -> CosimSession {
    CosimSession::new(SessionOptions {
        session_id: ID,
        sim_origin_ns: 4000,
        native_origin_ns: 900,
        required_capabilities: CAP_STEP | CAP_IMU | CAP_ADC,
    })
    .unwrap()
}
fn pair() -> (StreamTransport<UnixStream>, UnixStream) {
    let (host, peer) = UnixStream::pair().unwrap();
    host.set_nonblocking(true).unwrap();
    peer.set_read_timeout(Some(std::time::Duration::from_secs(1)))
        .unwrap();
    (StreamTransport::new(host, 8).unwrap(), peer)
}
fn read(peer: &mut UnixStream) -> Frame {
    let mut length = [0; 4];
    peer.read_exact(&mut length).unwrap();
    let mut body = vec![0; u32::from_le_bytes(length) as usize];
    peer.read_exact(&mut body).unwrap();
    Frame::decode_body(&body).unwrap()
}
fn frame(kind: Kind, id: u64, time: u64, dt: u64, payload: Vec<u8>) -> Frame {
    Frame {
        kind,
        step_id: id,
        time_ns: time,
        dt_ns: dt,
        session_id: ID,
        payload,
    }
}
fn bytes(frame: &Frame) -> Vec<u8> {
    let body = frame.encode_body().unwrap();
    let mut bytes = (body.len() as u32).to_le_bytes().to_vec();
    bytes.extend(body);
    bytes
}
fn reset_ack() -> Frame {
    frame(
        Kind::ResetAck,
        0,
        4000,
        0,
        ResetAck {
            status: 0,
            capabilities: 11,
            virtual_time_ns: 900,
            queue_depth: 0,
        }
        .encode()
        .unwrap(),
    )
}
fn ack(id: u64, time: u64) -> Frame {
    frame(
        Kind::StepAck,
        id,
        time,
        1000,
        StepAck {
            status: 0,
            queue_depth: 0,
            dropped_count: 0,
        }
        .encode()
        .unwrap(),
    )
}
fn imu() -> StepInput {
    StepInput::CompactImu(ImuSample {
        gyro_dps: [0.; 3],
        accel_g: [0., 0., 1.],
        gyro_bias_dps: [0.; 3],
        accel_bias_g: [0.; 3],
        sensor_time_ns: 0,
        sample_id: 0,
    })
}
fn adc() -> StepInput {
    StepInput::Sections(vec![InputSection::Adc(AdcInput {
        channel: 0,
        raw: 1234,
    })])
}
fn start(
    session: &mut CosimSession,
    stream: &mut StreamTransport<UnixStream>,
    peer: &mut UnixStream,
) {
    session.begin_reset(stream).unwrap();
    assert_eq!(read(peer), frame(Kind::Reset, 0, 4000, 0, vec![]));
    peer.write_all(&bytes(&reset_ack())).unwrap();
    session.pump(stream, |_| Ok(())).unwrap();
    assert!(session.started());
}
#[test]
fn fragmented_open_reset_adc_ack_and_imu_done_are_distinct_native_obligations() {
    let (mut stream, mut peer) = pair();
    let mut session = session();
    session.begin_reset(&mut stream).unwrap();
    read(&mut peer);
    let opening = V1Frame {
        kind: 3,
        sequence: 1,
        time_ns: 0,
        payload: vec![0; 12],
    }
    .encode_body()
    .unwrap();
    let mut response = (opening.len() as u32).to_le_bytes().to_vec();
    response.extend(opening);
    response.extend(bytes(&reset_ack()));
    peer.write_all(&response[..3]).unwrap();
    assert_eq!(session.pump(&mut stream, |_| Ok(())).unwrap(), 0);
    assert!(!session.started());
    peer.write_all(&response[3..]).unwrap();
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert!(session.started());
    let first = session.submit(&mut stream, 5000, adc()).unwrap();
    let sent = read(&mut peer);
    assert_eq!((sent.step_id, sent.time_ns, sent.dt_ns), (1, 5000, 1000));
    peer.write_all(&bytes(&ack(first, 5000))).unwrap();
    session
        .pump(&mut stream, |event| {
            assert!(matches!(event.response, Response::StepAck(_)));
            Ok(())
        })
        .unwrap();
    assert_eq!(session.outstanding(), 0);
    let second = session.submit(&mut stream, 6000, imu()).unwrap();
    read(&mut peer);
    peer.write_all(&bytes(&ack(second, 6000))).unwrap();
    session.pump(&mut stream, |_| Ok(())).unwrap();
    assert!(session.acknowledged(second));
    assert_eq!(session.outstanding(), 1);
    let done = StepDone {
        status: 2,
        consumed_mask: 1,
        missing_mask: 2,
        queue_depth: 0,
        dropped_count: 0,
    };
    peer.write_all(&bytes(&frame(
        Kind::StepDone,
        second,
        6000,
        1000,
        done.encode().unwrap(),
    )))
    .unwrap();
    let mut observed = vec![];
    session
        .pump(&mut stream, |event| {
            observed.push(event.response);
            Ok(())
        })
        .unwrap();
    assert_eq!(observed, vec![Response::StepDone(done)]);
    assert_eq!(session.outstanding(), 0);
}
#[test]
fn invalid_reset_or_foreign_session_latches_before_consuming_the_later_physical_frame() {
    for kind in [0, 1, 2] {
        let (mut stream, mut peer) = pair();
        let mut session = session();
        session.begin_reset(&mut stream).unwrap();
        read(&mut peer);
        let mut invalid = reset_ack();
        match kind {
            0 => invalid.session_id += 1,
            1 => {
                invalid.payload = ResetAck {
                    status: 0,
                    capabilities: 11,
                    virtual_time_ns: 901,
                    queue_depth: 0,
                }
                .encode()
                .unwrap()
            }
            _ => {
                invalid.payload = ResetAck {
                    status: 0,
                    capabilities: 1,
                    virtual_time_ns: 900,
                    queue_depth: 0,
                }
                .encode()
                .unwrap()
            }
        }
        let suffix = bytes(&reset_ack());
        let mut batch = bytes(&invalid);
        batch.extend(&suffix);
        peer.write_all(&batch).unwrap();
        let error = session
            .pump(&mut stream, |_| panic!("invalid response published"))
            .unwrap_err();
        assert_eq!(stream.buffered_bytes(), suffix.len());
        assert!(!session.started());
        assert!(Arc::ptr_eq(
            &error,
            &session.pump(&mut stream, |_| Ok(())).unwrap_err()
        ));
    }
}
#[test]
fn duplicate_ack_or_done_without_its_ordered_imu_acceptance_is_terminal() {
    for mode in [
        "duplicate-ack",
        "adc-done",
        "done-before-ack",
        "out-of-order-done",
        "ack-status",
    ] {
        let (mut stream, mut peer) = pair();
        let mut session = session();
        start(&mut session, &mut stream, &mut peer);
        let has_imu = mode != "adc-done";
        session
            .submit(&mut stream, 5000, if has_imu { imu() } else { adc() })
            .unwrap();
        read(&mut peer);
        let first = ack(1, 5000);
        if mode != "done-before-ack" && mode != "ack-status" {
            peer.write_all(&bytes(&first)).unwrap();
            session.pump(&mut stream, |_| Ok(())).unwrap();
        }
        let invalid = match mode {
            "duplicate-ack" => first,
            "ack-status" => frame(
                Kind::StepAck,
                1,
                5000,
                1000,
                StepAck {
                    status: 1,
                    queue_depth: 0,
                    dropped_count: 0,
                }
                .encode()
                .unwrap(),
            ),
            "out-of-order-done" => {
                session.submit(&mut stream, 6000, imu()).unwrap();
                read(&mut peer);
                peer.write_all(&bytes(&ack(2, 6000))).unwrap();
                session.pump(&mut stream, |_| Ok(())).unwrap();
                frame(
                    Kind::StepDone,
                    2,
                    6000,
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
            _ => frame(
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
            ),
        };
        peer.write_all(&bytes(&invalid)).unwrap();
        let error = session
            .pump(&mut stream, |_| panic!("bad response published"))
            .unwrap_err();
        assert!(Arc::ptr_eq(&error, session.failure().unwrap()), "{mode}");
    }
}
#[test]
fn outstanding_imu_capacity_rejects_before_new_io_and_close_leaves_the_stream_borrowed() {
    let (mut stream, mut peer) = pair();
    let mut session = session();
    start(&mut session, &mut stream, &mut peer);
    for id in 1..=64 {
        session
            .submit(&mut stream, 4000 + id * 1000, imu())
            .unwrap();
        read(&mut peer);
        peer.write_all(&bytes(&ack(id, 4000 + id * 1000))).unwrap();
        session.pump(&mut stream, |_| Ok(())).unwrap();
    }
    assert!(session.submit(&mut stream, 69000, imu()).is_err());
    assert_eq!(session.outstanding(), 64);
    session.close();
    assert!(session.pump(&mut stream, |_| Ok(())).is_err());
    // The physical owner is still usable after logical cancellation.
    stream.send(b"still-owned".to_vec()).unwrap();
    let mut received = [0; 11];
    peer.read_exact(&mut received).unwrap();
    assert_eq!(&received, b"still-owned");
}

#[test]
#[ignore = "requires shared harness COSIM_SOCKET to an original paused native QEMU"]
fn native_session_reset_adc_acceptance_and_unconsumed_imu_obligation() {
    use std::{
        path::PathBuf,
        time::{Duration, Instant},
    };
    let socket = UnixStream::connect(PathBuf::from(
        std::env::var_os("COSIM_SOCKET").expect("set COSIM_SOCKET"),
    ))
    .unwrap();
    socket.set_nonblocking(true).unwrap();
    let mut stream = StreamTransport::new(socket, 8).unwrap();
    let mut session = CosimSession::new(SessionOptions {
        session_id: ID,
        sim_origin_ns: 4000,
        native_origin_ns: 0,
        required_capabilities: 11,
    })
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    session.begin_reset(&mut stream).unwrap();
    while !session.started() {
        session.pump(&mut stream, |_| Ok(())).unwrap();
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    let adc = session.submit(&mut stream, 5000, adc()).unwrap();
    while !session.acknowledged(adc) {
        session.pump(&mut stream, |_| Ok(())).unwrap();
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(session.outstanding(), 0);
    let imu = session.submit(&mut stream, 6000, imu()).unwrap();
    while !session.acknowledged(imu) {
        session.pump(&mut stream, |_| Ok(())).unwrap();
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(session.outstanding(), 1);
    session.close();
}
