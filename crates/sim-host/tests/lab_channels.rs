#![cfg(feature = "lab")]
#[cfg(unix)]
use sim_host::{
    can::CanFrame,
    lab::channels::{ChannelData, ChannelEvent},
    transports::fixed84::{self, CanStream},
};
use sim_host::{
    lab::channels::{AdcValue, LabChannels},
    signals::ImuSample,
    step_wire::V1Frame,
    transports::stream::StreamTransport,
};
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::{
    collections::VecDeque,
    io::{self, Read, Write},
};

#[cfg(unix)]
fn pair() -> (UnixStream, UnixStream) {
    let (host, peer) = UnixStream::pair().unwrap();
    host.set_nonblocking(true).unwrap();
    peer.set_nonblocking(true).unwrap();
    (host, peer)
}
#[cfg(unix)]
fn drain(peer: &mut UnixStream) -> Vec<u8> {
    let mut bytes = Vec::new();
    match peer.read_to_end(&mut bytes) {
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => (),
        result => panic!("unexpected peer read: {result:?}"),
    }
    bytes
}
#[cfg(unix)]
fn frame(stamp: u64, byte: u8) -> CanFrame {
    CanFrame::new(0x321, 0, Some(1), vec![byte], stamp).unwrap()
}
#[cfg(unix)]
fn send_v1(peer: &mut UnixStream, kind: u16, time_ns: u64) {
    let body = V1Frame {
        kind,
        sequence: 0,
        time_ns,
        payload: if kind == 3 { vec![0; 12] } else { vec![] },
    }
    .encode_body()
    .unwrap();
    peer.write_all(&(body.len() as u32).to_le_bytes()).unwrap();
    peer.write_all(&body).unwrap();
}
fn decode_v1(bytes: &[u8]) -> Vec<V1Frame> {
    let mut rest = bytes;
    let mut frames = Vec::new();
    while !rest.is_empty() {
        let length = u32::from_le_bytes(rest[..4].try_into().unwrap()) as usize;
        frames.push(V1Frame::decode_body(&rest[4..4 + length]).unwrap());
        rest = &rest[4 + length..];
    }
    frames
}

#[test]
#[cfg(unix)]
fn channels_keep_uart_poll_time_can_wire_time_and_physical_tail_across_reset() {
    let (uart, mut uart_peer) = pair();
    let mut uart = StreamTransport::new(uart, 256).unwrap();
    let (can, mut can_peer) = pair();
    let mut can = CanStream::new(can, 256).unwrap();
    let mut channels = LabChannels::new(
        vec![("uart1".into(), &mut uart)],
        vec![("can1".into(), &mut can)],
        None,
    )
    .unwrap();
    channels.reset(100).unwrap();
    channels.send_uart("uart1", b"\0\xff").unwrap();
    assert_eq!(drain(&mut uart_peer), b"\0\xff");
    channels.send_can("can1", frame(101, 0xaa)).unwrap();
    uart_peer.write_all(b"ready\n").unwrap();
    can_peer
        .write_all(&fixed84::encode(&frame(90, 0xbb)))
        .unwrap();
    can_peer
        .write_all(&fixed84::encode(&frame(120, 0xcc)))
        .unwrap();
    assert_eq!(
        channels.service(110, true).unwrap(),
        vec![
            ChannelEvent {
                endpoint: "uart1".into(),
                timestamp_ns: 110,
                data: ChannelData::Uart(b"ready\n".to_vec())
            },
            ChannelEvent {
                endpoint: "can1".into(),
                timestamp_ns: 90,
                data: ChannelData::Can(frame(90, 0xbb))
            },
        ]
    );
    assert_eq!(drain(&mut can_peer), fixed84::encode(&frame(110, 0xaa)));
    assert!(channels.send_can("can1", frame(109, 0)).is_err());
    assert!(channels.service(119, true).unwrap().is_empty());
    assert_eq!(
        channels.service(120, true).unwrap(),
        vec![ChannelEvent {
            endpoint: "can1".into(),
            timestamp_ns: 120,
            data: ChannelData::Can(frame(120, 0xcc))
        }]
    );
    let pending = fixed84::encode(&frame(100, 0xdd));
    can_peer.write_all(&pending[..19]).unwrap();
    assert!(channels.service(121, true).unwrap().is_empty());
    channels.reset(130).unwrap();
    can_peer.write_all(&pending[19..]).unwrap();
    assert_eq!(
        channels.service(131, true).unwrap(),
        vec![ChannelEvent {
            endpoint: "can1".into(),
            timestamp_ns: 100,
            data: ChannelData::Can(frame(100, 0xdd))
        }]
    );
    assert!(channels.service(130, true).is_err());
    assert!(drain(&mut can_peer).is_empty());
}

#[test]
#[cfg(unix)]
fn v1_adc_requires_opening_explicit_reset_and_real_increasing_timestamps() {
    let (cosim, mut peer) = pair();
    let mut cosim = StreamTransport::new(cosim, 256).unwrap();
    let mut channels = LabChannels::new(vec![], vec![], Some(&mut cosim)).unwrap();
    assert!(channels.reset(100).is_err());
    assert!(drain(&mut peer).is_empty());
    assert!(!channels.consume_opening(false).unwrap());
    send_v1(&mut peer, 3, 0);
    assert!(channels.consume_opening(false).unwrap());
    channels.reset(100).unwrap();
    assert!(channels.send_adc(4, AdcValue::Raw(12345), 100).is_err());
    channels.send_adc(4, AdcValue::Raw(12345), 101).unwrap();
    assert!(
        channels
            .send_adc(4, AdcValue::Voltage(3_300_001), 102)
            .is_err()
    );
    channels
        .send_adc(4, AdcValue::Voltage(1_650_000), 102)
        .unwrap();
    let expected = vec![
        V1Frame {
            kind: 1,
            sequence: 0,
            time_ns: 100,
            payload: vec![],
        },
        V1Frame {
            kind: 5,
            sequence: 1,
            time_ns: 101,
            payload: vec![4, 0, 0x39, 0x30, 0, 0, 0, 0],
        },
        V1Frame {
            kind: 6,
            sequence: 2,
            time_ns: 102,
            payload: vec![4, 0, 0, 0, 0x50, 0x2d, 0x19, 0, 0, 0, 0, 0],
        },
    ];
    assert_eq!(decode_v1(&drain(&mut peer)), expected);
    send_v1(&mut peer, 1, 0);
    channels.service(103, true).unwrap();
    assert!(channels.send_adc(4, AdcValue::Raw(0), 104).is_err());
    assert!(drain(&mut peer).is_empty());
    channels.reset(200).unwrap();
    channels.send_adc(4, AdcValue::Raw(0), 201).unwrap();
    let frames = decode_v1(&drain(&mut peer));
    assert_eq!(
        (frames[0].kind, frames[1].sequence, frames[1].time_ns),
        (1, 1, 201)
    );
}

#[test]
#[cfg(unix)]
fn imu_and_adc_share_admission_order_and_freeze_after_native_reset() {
    let (cosim, mut peer) = pair();
    let mut cosim = StreamTransport::new(cosim, 256).unwrap();
    let mut channels = LabChannels::new(vec![], vec![], Some(&mut cosim)).unwrap();
    let sample = ImuSample {
        gyro_rad_s: [std::f64::consts::PI, -std::f64::consts::FRAC_PI_2, 0.0],
        accel_m_s2: [0.0, 0.0, 9.80665],
    };
    assert!(channels.send_imu(&sample, 100).is_err());
    send_v1(&mut peer, 3, 0);
    assert!(channels.consume_opening(false).unwrap());
    channels.reset(100).unwrap();
    assert!(channels.send_imu(&sample, 100).is_err());
    assert!(
        channels
            .send_imu(
                &ImuSample {
                    gyro_rad_s: [f64::NAN, 0.0, 0.0],
                    ..sample
                },
                101
            )
            .is_err()
    );
    channels.send_imu(&sample, 101).unwrap();
    assert!(channels.send_adc(4, AdcValue::Raw(12345), 101).is_err());
    channels.send_adc(4, AdcValue::Raw(12345), 102).unwrap();
    assert!(channels.send_imu(&sample, 102).is_err());
    channels.send_imu(&sample, 103).unwrap();
    let frames = decode_v1(&drain(&mut peer));
    assert_eq!(
        frames
            .iter()
            .map(|frame| (frame.kind, frame.sequence, frame.time_ns))
            .collect::<Vec<_>>(),
        [(1, 0, 100), (2, 1, 101), (5, 2, 102), (2, 3, 103)]
    );
    // IEEE754 binary32 literals for 180, -90, 0 degrees/s and +1g, independent
    // of the conversion/serialization helpers used by the producer.
    assert_eq!(
        frames[1].payload,
        [
            0, 0, 0x34, 0x43, 0, 0, 0xb4, 0xc2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x80,
            0x3f,
        ]
    );
    send_v1(&mut peer, 1, 80);
    assert_eq!(
        channels.service(104, true).unwrap(),
        [ChannelEvent {
            endpoint: "cosim".into(),
            timestamp_ns: 80,
            data: ChannelData::Reset,
        }]
    );
    assert!(channels.send_imu(&sample, 105).is_err());
    assert!(channels.send_adc(4, AdcValue::Raw(0), 105).is_err());
    assert!(drain(&mut peer).is_empty());
}

// Captured from pinned QEMU's prebound TCP client at startup: OPENED telemetry,
// initial machine RESET, then post-reset telemetry. Expected bytes come from
// the native producer, independently of the Rust encoder under test.
const EARLY_OPENING: &[u8] = include_bytes!("fixtures/cosim_early_opening.bin");

struct ReadBatches {
    pending: VecDeque<Vec<u8>>,
    written: Vec<u8>,
}
impl Read for ReadBatches {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let Some(batch) = self.pending.pop_front() else {
            return Err(io::ErrorKind::WouldBlock.into());
        };
        assert!(batch.len() <= output.len());
        output[..batch.len()].copy_from_slice(&batch);
        Ok(batch.len())
    }
}
impl Write for ReadBatches {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.written.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn early_opening_waits_for_machine_reset_and_following_telemetry() {
    // Separate before RESET, inside RESET, after RESET, and inside the final
    // telemetry. Coalesced delivery is the originally reported load failure.
    for split in [44, 47, 76, 119, 120] {
        let mut batches = vec![EARLY_OPENING[..split].to_vec()];
        if split < EARLY_OPENING.len() {
            batches.push(EARLY_OPENING[split..].to_vec());
        }
        let io = ReadBatches {
            pending: batches.into(),
            written: Vec::new(),
        };
        let mut stream = StreamTransport::new(io, 8).unwrap();
        let mut channels = LabChannels::new(vec![], vec![], Some(&mut stream)).unwrap();
        assert_eq!(channels.consume_opening(true).unwrap(), split == 120);
        if split < 120 {
            assert!(channels.reset(1).is_err());
            assert!(channels.consume_opening(true).unwrap());
        }
        channels.reset(1).unwrap();
        channels.service(2, true).unwrap();
        channels.send_adc(4, AdcValue::Raw(12345), 2).unwrap();
        drop(channels);
        let frames = decode_v1(&stream.io().written);
        assert_eq!(
            frames
                .iter()
                .map(|f| (f.kind, f.sequence, f.time_ns))
                .collect::<Vec<_>>(),
            [(1, 0, 1), (5, 1, 2)]
        );
    }
}

#[test]
fn opening_rejects_non_startup_frames_and_invalid_telemetry() {
    let mut ack = EARLY_OPENING[44..76].to_vec();
    ack[10] = 4; // Valid v1 ACK, which cannot open the Lab ADC session.
    let mut invalid_telemetry = EARLY_OPENING[..44].to_vec();
    invalid_telemetry[42] = 1; // Canonical telemetry's reserved field.
    for bytes in [ack, invalid_telemetry] {
        let mut stream = StreamTransport::new(
            ReadBatches {
                pending: [bytes].into(),
                written: Vec::new(),
            },
            8,
        )
        .unwrap();
        let mut channels = LabChannels::new(vec![], vec![], Some(&mut stream)).unwrap();
        assert!(channels.consume_opening(true).is_err());
        assert!(channels.reset(1).is_err());
    }
}
