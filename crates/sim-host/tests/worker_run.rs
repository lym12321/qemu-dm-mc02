#![cfg(all(target_os = "linux", feature = "worker"))]
use sim_host::{
    step_wire::{Frame, Kind, ResetAck, StepAck, StepDone, V1Frame},
    worker::{self, Protocol, WorkerOptions},
};
use std::{
    io::{Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    sync::mpsc,
    time::Duration,
};
fn body(peer: &mut UnixStream) -> Vec<u8> {
    let mut n = [0; 4];
    peer.read_exact(&mut n).unwrap();
    let mut b = vec![0; u32::from_le_bytes(n) as usize];
    peer.read_exact(&mut b).unwrap();
    b
}
fn write(peer: &mut UnixStream, b: &[u8]) {
    peer.write_all(&(b.len() as u32).to_le_bytes()).unwrap();
    peer.write_all(b).unwrap();
}
fn send(peer: &mut UnixStream, f: &Frame) {
    write(peer, &f.encode_body().unwrap());
}
fn reply(request: &Frame, kind: Kind, payload: Vec<u8>) -> Frame {
    Frame {
        kind,
        payload,
        ..request.clone()
    }
}
fn reset_ack(peer: &mut UnixStream, r: &Frame, native: u64) {
    send(
        peer,
        &reply(
            r,
            Kind::ResetAck,
            ResetAck {
                status: 0,
                capabilities: 11,
                virtual_time_ns: native,
                queue_depth: 0,
            }
            .encode()
            .unwrap(),
        ),
    );
}
fn ack(peer: &mut UnixStream, r: &Frame, status: u32) {
    send(
        peer,
        &reply(
            r,
            Kind::StepAck,
            StepAck {
                status,
                queue_depth: 0,
                dropped_count: 0,
            }
            .encode()
            .unwrap(),
        ),
    );
}
fn done(peer: &mut UnixStream, r: &Frame) {
    send(
        peer,
        &reply(
            r,
            Kind::StepDone,
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
    );
}
fn setup() -> (tempfile::TempDir, UnixListener, WorkerOptions) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cosim.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let mut options = WorkerOptions::new(path);
    options.frames = 1;
    options.connect_timeout = Duration::from_secs(2);
    options.host_timeout = Some(Duration::from_secs(8));
    (dir, listener, options)
}
#[test]
fn v1_open_origin_adc_subticks_and_333hz_timestamps() {
    let (_dir, listener, mut options) = setup();
    options.rate = 333.0;
    options.frames = 3;
    options.adc_input = vec![(2, 1234)];
    options.adc_voltage = vec![(7, 2_500_000)];
    let (stop, stopped) = mpsc::channel();
    let peer = std::thread::spawn(move || {
        let (mut p, _) = listener.accept().unwrap();
        p.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        write(
            &mut p,
            &V1Frame {
                kind: 3,
                sequence: 1,
                time_ns: 700,
                payload: vec![0; 12],
            }
            .encode_body()
            .unwrap(),
        );
        let reset = V1Frame::decode_body(&body(&mut p)).unwrap();
        assert_eq!((reset.kind, reset.time_ns), (1, 700));
        let mut seq = 0;
        let mut previous = 700;
        for time in [3_003_703, 6_006_706, 9_009_709] {
            let raw = V1Frame::decode_body(&body(&mut p)).unwrap();
            seq += 1;
            assert_eq!(
                (raw.kind, raw.sequence, raw.time_ns),
                (5, seq, previous + 1)
            );
            assert_eq!(raw.payload, [2, 0, 0xd2, 4, 0, 0, 0, 0]);
            let volts = V1Frame::decode_body(&body(&mut p)).unwrap();
            seq += 1;
            assert_eq!(
                (volts.kind, volts.sequence, volts.time_ns),
                (6, seq, previous + 2)
            );
            assert_eq!(volts.payload, [7, 0, 0, 0, 0xa0, 0x25, 0x26, 0, 0, 0, 0, 0]);
            let imu = V1Frame::decode_body(&body(&mut p)).unwrap();
            seq += 1;
            assert_eq!((imu.kind, imu.sequence, imu.time_ns), (2, seq, time));
            assert_eq!(&imu.payload[20..], [0, 0, 0x80, 0x3f]);
            previous = time;
        }
        let _ = stopped.recv_timeout(Duration::from_secs(3));
    });
    let result = worker::run(options);
    stop.send(()).unwrap();
    peer.join().unwrap();
    let report = result.unwrap();
    assert_eq!((report.frames, report.final_time_ns), (3, 9_009_709));
}
#[test]
fn v2_lost_ack_queue_full_and_done_replay_exact_bytes() {
    let (_dir, listener, mut options) = setup();
    options.protocol = Protocol::V2;
    options.wait_step_done = true;
    let (stop, stopped) = mpsc::channel();
    let peer = std::thread::spawn(move || {
        let (mut p, _) = listener.accept().unwrap();
        p.set_read_timeout(Some(Duration::from_secs(4))).unwrap();
        let reset = Frame::decode_body(&body(&mut p)).unwrap();
        reset_ack(&mut p, &reset, 900);
        let first = body(&mut p);
        let step = Frame::decode_body(&first).unwrap();
        assert_eq!(
            (step.step_id, step.time_ns, step.dt_ns),
            (1, 1_000_000, 1_000_000)
        );
        assert_eq!(body(&mut p), first);
        ack(&mut p, &step, 1);
        assert_eq!(body(&mut p), first);
        ack(&mut p, &step, 0);
        assert_eq!(body(&mut p), first);
        ack(&mut p, &step, 0);
        done(&mut p, &step);
        let _ = stopped.recv_timeout(Duration::from_secs(3));
    });
    let result = worker::run(options);
    stop.send(()).unwrap();
    peer.join().unwrap();
    let report = result.unwrap();
    assert_eq!(
        (
            report.frames,
            report.retries,
            report.completions,
            report.consumed_mask
        ),
        (1, 3, 1, 3)
    );
}
#[test]
fn unsolicited_reset_rebases_after_ack_and_restarts_sample_index() {
    let (_dir, listener, mut options) = setup();
    options.protocol = Protocol::V2;
    options.wait_step_done = true;
    let (stop, stopped) = mpsc::channel();
    let peer = std::thread::spawn(move || {
        let (mut p, _) = listener.accept().unwrap();
        p.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let reset = Frame::decode_body(&body(&mut p)).unwrap();
        reset_ack(&mut p, &reset, 900);
        let _first = Frame::decode_body(&body(&mut p)).unwrap();
        let reset = Frame {
            kind: Kind::Reset,
            step_id: 0,
            time_ns: 100,
            dt_ns: 0,
            session_id: reset.session_id.wrapping_add(1).max(1),
            payload: vec![],
        };
        send(&mut p, &reset);
        reset_ack(&mut p, &reset, 500);
        let step = Frame::decode_body(&body(&mut p)).unwrap();
        assert_eq!(
            (step.session_id, step.step_id, step.time_ns, step.dt_ns),
            (reset.session_id, 1, 1_000_100, 1_000_000)
        );
        ack(&mut p, &step, 0);
        done(&mut p, &step);
        let _ = stopped.recv_timeout(Duration::from_secs(3));
    });
    let result = worker::run(options);
    stop.send(()).unwrap();
    peer.join().unwrap();
    let report = result.unwrap();
    assert_eq!(
        (report.frames, report.sessions, report.final_time_ns),
        (1, 2, 1_000_100)
    );
}

#[test]
fn optional_done_preserves_advisory_missing_status() {
    let (_dir, listener, mut options) = setup();
    options.protocol = Protocol::V2;
    let (stop, stopped) = mpsc::channel();
    let peer = std::thread::spawn(move || {
        let (mut p, _) = listener.accept().unwrap();
        p.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let r = Frame::decode_body(&body(&mut p)).unwrap();
        reset_ack(&mut p, &r, 0);
        let step = Frame::decode_body(&body(&mut p)).unwrap();
        let mut batch = Vec::new();
        for frame in [
            reply(
                &step,
                Kind::StepAck,
                StepAck {
                    status: 0,
                    queue_depth: 0,
                    dropped_count: 0,
                }
                .encode()
                .unwrap(),
            ),
            reply(
                &step,
                Kind::StepDone,
                StepDone {
                    status: 2,
                    consumed_mask: 1,
                    missing_mask: 2,
                    queue_depth: 0,
                    dropped_count: 0,
                }
                .encode()
                .unwrap(),
            ),
        ] {
            let b = frame.encode_body().unwrap();
            batch.extend_from_slice(&(b.len() as u32).to_le_bytes());
            batch.extend(b);
        }
        p.write_all(&batch).unwrap();
        let _ = stopped.recv_timeout(Duration::from_secs(3));
    });
    let result = worker::run(options);
    stop.send(()).unwrap();
    peer.join().unwrap();
    let report = result.unwrap();
    assert_eq!(
        (
            report.frames,
            report.completions,
            report.consumed_mask,
            report.missing_mask
        ),
        (1, 1, 1, 2)
    );
}

#[cfg(feature = "devices")]
#[test]
fn cli_c620_worker_maps_native_feedback_and_parses_typed_adc_arguments() {
    use sim_host::{
        can::CanFrame,
        step_wire::{AdcInput, AdcVoltage, InputSection, StepInput},
        transports::fixed84,
    };
    let (dir, listener, options) = setup();
    let can_path = dir.path().join("can.sock");
    let can_listener = UnixListener::bind(&can_path).unwrap();
    let (stop, stopped) = mpsc::channel();
    let peer = std::thread::spawn(move || {
        let (mut p, _) = listener.accept().unwrap();
        p.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let (mut can, _) = can_listener.accept().unwrap();
        can.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let r = Frame::decode_body(&body(&mut p)).unwrap();
        reset_ack(&mut p, &r, 900);
        can.write_all(&fixed84::encode(
            &CanFrame::new(0x200, 0, None, vec![0x10, 0, 0, 0, 0, 0, 0, 0], 900).unwrap(),
        ))
        .unwrap();
        for (index, current) in [4096u16, 0].into_iter().enumerate() {
            let step = Frame::decode_body(&body(&mut p)).unwrap();
            let StepInput::Sections(sections) = StepInput::decode(&step.payload).unwrap() else {
                panic!("CLI ADC arguments must reach typed STEP sections")
            };
            assert_eq!(
                &sections[1..],
                &[
                    InputSection::Adc(AdcInput {
                        channel: 2,
                        raw: 1234
                    }),
                    InputSection::Voltage(AdcVoltage {
                        channel: 7,
                        flags: 0,
                        voltage_uv: 1_650_000,
                    }),
                ]
            );
            let mut bytes = [0; 84];
            can.read_exact(&mut bytes).unwrap();
            let feedback = fixed84::decode(&bytes).unwrap();
            assert_eq!(
                (feedback.can_id(), feedback.timestamp_ns()),
                (0x201, (index as u64 + 1) * 1_000_000 + 900)
            );
            assert_eq!(&feedback.data()[4..6], current.to_be_bytes());
            if index == 0 {
                can.write_all(&fixed84::encode(
                    &CanFrame::new(0x200, 0, None, vec![0; 8], 1_000_900).unwrap(),
                ))
                .unwrap();
            }
            ack(&mut p, &step, 0);
        }
        let _ = stopped.recv_timeout(Duration::from_secs(3));
    });
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_sim"))
        .args(["worker", "--cosim"])
        .arg(options.cosim)
        .arg("--fdcan")
        .arg(can_path)
        .args([
            "--can-profile",
            "rotor",
            "--dji-id",
            "1",
            "--protocol",
            "v2",
            "--frames",
            "2",
            "--timeout",
            "8",
            "--adc-input",
            "2:0x4d2",
            "--adc-voltage",
            "7:1.6500005",
        ])
        .output()
        .unwrap();
    stop.send(()).unwrap();
    peer.join().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let output = String::from_utf8(result.stdout).unwrap();
    assert!(
        output.contains("Worker finished: frames=2 sessions=1 retries=0 acknowledgements=2"),
        "{output}"
    );
}
#[test]
fn admission_rejects_unbounded_or_unsupported_options_before_connect() {
    let mut o = WorkerOptions::new("/missing");
    o.rate = f64::NAN;
    assert!(worker::run(o).unwrap_err().to_string().contains("rate"));
    let mut o = WorkerOptions::new("/missing");
    o.wait_step_done = true;
    assert!(worker::run(o)
        .unwrap_err()
        .to_string()
        .contains("requires v2"));
}

#[cfg(feature = "devices")]
#[test]
fn can_observer_reads_completed_cuts_and_its_failure_remains_primary() {
    use sim_host::{
        can::CanFrame,
        transports::fixed84,
        worker::can_graph::{CanProfile, RotorProfile},
    };
    use std::sync::{Arc, Mutex};
    let missing = WorkerOptions::new("/missing");
    assert!(worker::run_with_can_observer(missing, |_| Ok(()))
        .unwrap_err()
        .to_string()
        .contains("requires a C620 profile"));
    let (dir, listener, mut options) = setup();
    let can_path = dir.path().join("can.sock");
    let can_listener = UnixListener::bind(&can_path).unwrap();
    options.fdcan = Some(can_path);
    options.protocol = Protocol::V2;
    options.frames = 2;
    options.can_profile = Some(CanProfile::Rotor(RotorProfile {
        motor_ids: vec![1],
        ..Default::default()
    }));
    let peer = std::thread::spawn(move || {
        let (mut p, _) = listener.accept().unwrap();
        p.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let (mut can, _) = can_listener.accept().unwrap();
        let reset = Frame::decode_body(&body(&mut p)).unwrap();
        can.write_all(&fixed84::encode(
            &CanFrame::new(0x200, 0, None, vec![0, 123, 0, 0, 0, 0, 0, 0], 900).unwrap(),
        ))
        .unwrap();
        reset_ack(&mut p, &reset, 900);
        let step = Frame::decode_body(&body(&mut p)).unwrap();
        ack(&mut p, &step, 0);
        let mut byte = [0];
        assert_eq!(p.read(&mut byte).unwrap(), 0);
    });
    let seen = Arc::new(Mutex::new(Vec::new()));
    let observations = seen.clone();
    let primary =
        Arc::new(sim_core::Error::State("observer sink failed".into())) as sim_core::Failure;
    let chosen = primary.clone();
    let result = worker::run_with_can_observer(options, move |cut| {
        observations.lock().unwrap().push((
            cut.step_id,
            cut.session.t_sim_ns,
            cut.mapper.qemu_origin_ns,
            cut.motor_current_raw(1)?,
        ));
        if cut.step_id == 2 {
            Err(chosen.clone())
        } else {
            Ok(())
        }
    })
    .unwrap_err();
    peer.join().unwrap();
    assert!(Arc::ptr_eq(&result, &primary));
    assert_eq!(
        *seen.lock().unwrap(),
        [
            (0, 0, 900, 0),
            (1, 1_000_000, 900, 123),
            (2, 2_000_000, 900, 123)
        ]
    );
}

#[test]
fn injected_engine_is_admitted_before_creation_and_cleaned_after_connect_failure() {
    use sim_host::worker::{
        engine::{EngineSample, WorkerEngine},
        motor_protocol::MotorCommand,
    };
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    struct Owned(Arc<AtomicUsize>);
    impl WorkerEngine for Owned {
        fn reset(&mut self) -> sim_core::Result<()> {
            Ok(())
        }
        fn command(&mut self, _: MotorCommand) -> sim_core::Result<()> {
            Ok(())
        }
        fn step(&mut self, _: f64) -> sim_core::Result<EngineSample> {
            panic!("no connection")
        }
        fn close(&mut self) -> sim_core::Result<()> {
            self.0.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }
    let mut invalid = WorkerOptions::new("/missing");
    invalid.rate = f64::NAN;
    assert!(
        worker::run_with_engine(invalid, |_| panic!("admission must precede creation")).is_err()
    );
    let dir = tempfile::tempdir().unwrap();
    let mut options = WorkerOptions::new(dir.path().join("absent"));
    options.connect_timeout = Duration::from_millis(10);
    let closed = Arc::new(AtomicUsize::new(0));
    let result = worker::run_with_engine(options, |_| Ok(Box::new(Owned(closed.clone()))));
    assert!(result.unwrap_err().to_string().contains("deadline"));
    assert_eq!(closed.load(Ordering::Relaxed), 1);
    #[cfg(feature = "devices")]
    {
        use sim_host::worker::can_graph::{CanProfile, RotorProfile};
        let mut options = WorkerOptions::new("/missing");
        options.fdcan = Some("/missing-can".into());
        options.can_profile = Some(CanProfile::Rotor(RotorProfile::default()));
        assert!(
            worker::run_with_engine(options, |_| panic!("C620 owns its plant"))
                .unwrap_err()
                .to_string()
                .contains("cannot own a C620")
        );
    }
}

#[test]
#[ignore = "requires shared native QEMU harness COSIM_SOCKET; IMU consumer starts after first admitted STEP"]
fn native_v2_requires_actual_both_die_consumption() {
    let mut o = WorkerOptions::new(std::env::var_os("COSIM_SOCKET").unwrap());
    o.protocol = Protocol::V2;
    o.wait_step_done = true;
    o.frames = 1;
    o.host_timeout = Some(Duration::from_secs(6));
    let report = worker::run(o).unwrap();
    assert_eq!(
        (
            report.frames,
            report.completions,
            report.consumed_mask,
            report.missing_mask
        ),
        (1, 1, 3, 0)
    );
    println!("native worker v2 {report:?}");
}
#[test]
#[ignore = "requires shared native paused QEMU harness COSIM_SOCKET"]
fn native_v1_sends_native_adc_and_imu_frames() {
    let mut o = WorkerOptions::new(std::env::var_os("COSIM_SOCKET").unwrap());
    o.frames = 3;
    o.adc_input = vec![(4, 12345)];
    o.adc_voltage = vec![(7, 1_650_000)];
    o.host_timeout = Some(Duration::from_secs(6));
    let report = worker::run(o).unwrap();
    assert_eq!(report.frames, 3);
    println!("native worker v1 {report:?}");
}
