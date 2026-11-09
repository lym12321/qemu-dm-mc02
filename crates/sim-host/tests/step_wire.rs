#![cfg(feature = "cosim-wire")]
use sim_host::step_wire::*;

fn hex(value: &str) -> Vec<u8> {
    value
        .split_whitespace()
        .flat_map(|word| {
            word.as_bytes()
                .chunks_exact(2)
                .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        })
        .collect()
}

#[test]
fn independent_literal_header_and_response_fields_reject_boundary_corruption() {
    // Fixed v2 body layout from cosim headers and the existing wire contract:
    // magic/u16 version/kind/u32 payload length/u64 step/time/dt/u32 session.
    let literal = hex("444d4332 0200 0300 03000000 2a00000000000000 e803000000000000 1900000000000000 07000000 616263");
    let frame = Frame {
        kind: Kind::StepAck,
        step_id: 42,
        time_ns: 1000,
        dt_ns: 25,
        session_id: 7,
        payload: b"abc".to_vec(),
    };
    assert_eq!(frame.encode_body().unwrap(), literal);
    assert_eq!(Frame::decode_body(&literal).unwrap(), frame);
    for (offset, byte) in [(0, 0), (4, 1), (6, 99), (8, 4)] {
        let mut invalid = literal.clone();
        invalid[offset] = byte;
        assert!(Frame::decode_body(&invalid).is_err());
    }
    assert!(Frame::decode_body(&literal[..literal.len() - 1]).is_err());
    let mut too_long = frame;
    too_long.payload = vec![0; 513];
    assert!(too_long.encode_body().is_err());
    too_long.payload.pop();
    assert_eq!(
        Frame::decode_body(&too_long.encode_body().unwrap()).unwrap(),
        too_long
    );

    let failed = hex("02000000 01000000 02000000 06000000 07000000");
    let done = StepDone {
        status: 2,
        consumed_mask: 1,
        missing_mask: 2,
        queue_depth: 6,
        dropped_count: 7,
    };
    assert_eq!(done.encode().unwrap(), failed);
    assert_eq!(StepDone::decode(&failed).unwrap(), done);
    for value in [
        StepDone { status: 0, ..done },
        StepDone {
            missing_mask: 1,
            ..done
        },
        StepDone {
            consumed_mask: 4,
            ..done
        },
    ] {
        assert!(value.encode().is_err());
    }
    let ack = hex("00000000 3f000000 0807060504030201 02000000 00000000");
    let value = ResetAck::decode(&ack).unwrap();
    assert_eq!(
        value,
        ResetAck {
            status: 0,
            capabilities: 63,
            virtual_time_ns: 0x0102030405060708,
            queue_depth: 2
        }
    );
    assert_eq!(value.encode().unwrap(), ack);
    for offset in [4, 20] {
        let mut bad = ack.clone();
        bad[offset] = 255;
        assert!(ResetAck::decode(&bad).is_err());
    }
    let mut reserved = StepAck {
        status: 1,
        queue_depth: 2,
        dropped_count: 3,
    }
    .encode()
    .unwrap();
    reserved[12] = 1;
    assert!(StepAck::decode(&reserved).is_err());
}

#[test]
fn typed_adc_sections_use_c_admission_and_keep_compact_discriminator() {
    let literal = hex("0100 4353 0300 0000 08000000 1f00 ffff 00000000");
    let raw = InputSection::Adc(AdcInput {
        channel: 31,
        raw: 65535,
    });
    let input = StepInput::Sections(vec![raw.clone()]);
    assert_eq!(input.encode().unwrap(), literal);
    assert_eq!(StepInput::decode(&literal).unwrap(), input);
    let mut legacy = literal.clone();
    legacy[2..4].fill(0);
    assert_eq!(StepInput::decode(&legacy).unwrap(), input);
    for (offset, byte) in [(4, 5), (6, 1), (8, 9), (12, 32), (16, 1)] {
        let mut bad = literal.clone();
        bad[offset] = byte;
        assert!(StepInput::decode(&bad).is_err());
    }
    assert!(StepInput::Sections(vec![raw.clone(), raw])
        .encode()
        .is_err());
    assert!(AdcVoltage {
        channel: 1,
        flags: 2,
        voltage_uv: 1
    }
    .encode()
    .is_err());
    assert!(AdcVoltage {
        channel: 1,
        flags: 1,
        voltage_uv: 3_300_001
    }
    .encode()
    .is_err());
    let imu = ImuSample {
        gyro_dps: [1.0, -2.0, 3.5],
        accel_g: [0.0, 0.0, 1.0],
        gyro_bias_dps: [0.0; 3],
        accel_bias_g: [0.0; 3],
        sensor_time_ns: 123,
        sample_id: 7,
    };
    let encoded = StepInput::CompactImu(imu.clone()).encode().unwrap();
    assert_eq!(&encoded[..12], hex("0000803f 000000c0 00006040"));
    assert_eq!(
        StepInput::decode(&encoded).unwrap(),
        StepInput::CompactImu(imu.clone())
    );
    assert!(
        StepInput::Sections(vec![InputSection::Imu(imu.clone()), InputSection::Imu(imu)])
            .encode()
            .is_err()
    );
    let mut nonfinite = encoded;
    nonfinite[..4].copy_from_slice(&f32::NAN.to_le_bytes());
    assert!(StepInput::decode(&nonfinite).is_err());
    // An explicitly marked total-60-byte section record must not become compact IMU.
    let mut sixty = literal;
    sixty[0] = 2;
    sixty.extend(hex("0200 0000 20000000"));
    sixty.extend([0; 32]);
    assert_eq!(sixty.len(), 60);
    assert!(StepInput::decode(&sixty).is_err()); // invalid motor payload, not a finite IMU sample
}

#[test]
fn motor_order_finite_values_and_legacy_body_have_independent_literals() {
    let bytes = hex("0200 0900 0100 0000a0bf 0000 00002040");
    let command = MotorCommand {
        flags: 9,
        commands: vec![
            MotorCommandItem {
                index: 1,
                command: -1.25,
            },
            MotorCommandItem {
                index: 0,
                command: 2.5,
            },
        ],
    };
    assert_eq!(command.encode().unwrap(), bytes);
    assert_eq!(MotorCommand::decode(&bytes).unwrap(), command);
    let mut duplicate = bytes.clone();
    duplicate[10..12].copy_from_slice(&1u16.to_le_bytes());
    assert!(MotorCommand::decode(&duplicate).is_err());
    let mut nan = bytes;
    nan[6..10].copy_from_slice(&f32::INFINITY.to_le_bytes());
    assert!(MotorCommand::decode(&nan).is_err());
    let bytes = hex("0100 0000 0000803f 000000c0 00004040");
    let state = MotorState {
        states: vec![MotorStateItem {
            index: 0,
            position: 1.0,
            velocity: -2.0,
            effort: 3.0,
        }],
    };
    assert_eq!(state.encode().unwrap(), bytes);
    assert_eq!(MotorState::decode(&bytes).unwrap(), state);
    let marker = hex("444d4332 0100 0100 00000000 0000000000000000 0807060504030201");
    let v1 = V1Frame {
        kind: 1,
        sequence: 0,
        time_ns: 0x0102030405060708,
        payload: vec![],
    };
    assert_eq!(v1.encode_body().unwrap(), marker);
    assert_eq!(V1Frame::decode_body(&marker).unwrap(), v1);
    assert!(V1Frame {
        kind: 5,
        sequence: 1,
        time_ns: 1,
        payload: vec![0; 7]
    }
    .encode_body()
    .is_err());
}

#[cfg(unix)]
#[test]
#[ignore = "requires the shared QEMU harness and COSIM_SOCKET"]
fn native_v2_reset_and_adc_ack_consume_canonical_rust_values() {
    use sim_host::transports::stream::StreamTransport;
    use std::{
        os::unix::net::UnixStream,
        time::{Duration, Instant},
    };
    let path = std::env::var_os("COSIM_SOCKET").expect("shared harness must supply COSIM_SOCKET");
    let mut socket = UnixStream::connect(path).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    socket
        .set_write_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut stream = StreamTransport::new(&mut socket, 8).unwrap();
    fn receive(stream: &mut StreamTransport<&mut UnixStream>) -> Vec<u8> {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if let Some(body) = stream.next_length_prefixed(28, 552).unwrap() {
                return body;
            }
            assert!(Instant::now() < deadline, "native co-sim response timeout");
            stream.read_once().unwrap();
        }
    }
    let opening = V1Frame::decode_body(&receive(&mut stream)).unwrap();
    // cosim_event(CHR_EVENT_OPENED) sends the first v1 telemetry snapshot;
    // RESET is emitted on reconnect. Consume it before selecting v2.
    assert_eq!((opening.kind, opening.sequence), (3, 1));
    let reset = Frame {
        kind: Kind::Reset,
        step_id: 0,
        time_ns: 0,
        dt_ns: 0,
        session_id: 0xa1b2c3d4,
        payload: vec![],
    };
    stream
        .send_length_prefixed(&reset.encode_body().unwrap(), 40, 552)
        .unwrap();
    loop {
        let frame = Frame::decode_body(&receive(&mut stream)).unwrap();
        if frame.kind == Kind::Telemetry {
            Telemetry::decode(&frame.payload).unwrap();
            continue;
        }
        assert_eq!(
            (frame.kind, frame.step_id, frame.time_ns, frame.session_id),
            (Kind::ResetAck, 0, 0, reset.session_id)
        );
        let ack = ResetAck::decode(&frame.payload).unwrap();
        assert_eq!(ack.status, 0);
        assert_eq!(ack.capabilities & 9, 9); // existing STEP and ADC capabilities
        break;
    }
    let input = Frame {
        kind: Kind::Step,
        step_id: 1,
        time_ns: 1_000_000,
        dt_ns: 1_000_000,
        session_id: reset.session_id,
        payload: StepInput::Sections(vec![InputSection::Adc(AdcInput {
            channel: 0,
            raw: 1234,
        })])
        .encode()
        .unwrap(),
    };
    stream
        .send_length_prefixed(&input.encode_body().unwrap(), 40, 552)
        .unwrap();
    loop {
        let frame = Frame::decode_body(&receive(&mut stream)).unwrap();
        if frame.kind == Kind::Telemetry {
            Telemetry::decode(&frame.payload).unwrap();
            continue;
        }
        assert_eq!(
            (
                frame.kind,
                frame.step_id,
                frame.time_ns,
                frame.dt_ns,
                frame.session_id
            ),
            (Kind::StepAck, 1, 1_000_000, 1_000_000, reset.session_id)
        );
        assert_eq!(StepAck::decode(&frame.payload).unwrap().status, 0);
        break;
    }
    // This is protocol admission only; ADC/CPU consumption is a later gate.
}
