#![cfg(all(target_os = "linux", feature = "lab"))]
use sim_host::{
    can::CanFrame,
    lab::channels::{AdcValue, ChannelData, ChannelEvent, LabChannels},
    step_wire::V1Frame,
    transports::{
        fixed84::{self, CanStream},
        stream::StreamTransport,
    },
};
use std::{
    io::{self, Read, Write},
    os::unix::net::UnixStream,
};

fn pair() -> (UnixStream, UnixStream) {
    let (host, peer) = UnixStream::pair().unwrap();
    host.set_nonblocking(true).unwrap();
    peer.set_nonblocking(true).unwrap();
    (host, peer)
}
fn drain(peer: &mut UnixStream) -> Vec<u8> {
    let mut bytes = Vec::new();
    match peer.read_to_end(&mut bytes) {
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => (),
        result => panic!("unexpected peer read: {result:?}"),
    }
    bytes
}
fn frame(stamp: u64, byte: u8) -> CanFrame {
    CanFrame::new(0x321, 0, Some(1), vec![byte], stamp).unwrap()
}
fn send_v1(peer: &mut UnixStream, kind: u16) {
    let body = V1Frame {
        kind,
        sequence: 0,
        time_ns: 0,
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
fn v1_adc_requires_opening_explicit_reset_and_real_increasing_timestamps() {
    let (cosim, mut peer) = pair();
    let mut cosim = StreamTransport::new(cosim, 256).unwrap();
    let mut channels = LabChannels::new(vec![], vec![], Some(&mut cosim)).unwrap();
    assert!(channels.reset(100).is_err());
    assert!(drain(&mut peer).is_empty());
    assert!(!channels.consume_opening().unwrap());
    send_v1(&mut peer, 3);
    assert!(channels.consume_opening().unwrap());
    channels.reset(100).unwrap();
    assert!(channels.send_adc(4, AdcValue::Raw(12345), 100).is_err());
    channels.send_adc(4, AdcValue::Raw(12345), 101).unwrap();
    assert!(channels
        .send_adc(4, AdcValue::Voltage(3_300_001), 102)
        .is_err());
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
    send_v1(&mut peer, 1);
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
