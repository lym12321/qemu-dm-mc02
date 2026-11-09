#![cfg(all(target_os = "linux", feature = "socketcan"))]
use sim_host::{
    can::{CanFrame, BRS, EXTENDED, FD, RTR},
    transports::{
        fixed84::CanFrameSink,
        socketcan::{decode, encode, CanDatagramIo, SocketCanTransport},
    },
};
use socketcan::{
    frame::{can_frame_default, canfd_frame_default, AsPtr},
    id::{CANFD_ESI, CANFD_FDF, CAN_EFF_FLAG, CAN_ERR_FLAG, CAN_RTR_FLAG},
    CanAnyFrame, CanRawFrame,
};
use std::{collections::VecDeque, io, sync::Arc};
fn frame(id: u32, flags: u32, dlc: Option<u8>, data: impl Into<Vec<u8>>, time: u64) -> CanFrame {
    CanFrame::new(id, flags, dlc, data, time).unwrap()
}
fn raw(frame: CanAnyFrame) -> CanRawFrame {
    match frame {
        CanAnyFrame::Normal(f) => CanRawFrame::Classic(*f.as_ref()),
        CanAnyFrame::Remote(f) => CanRawFrame::Classic(*f.as_ref()),
        CanAnyFrame::Fd(f) => CanRawFrame::Fd(*f.as_ref()),
        CanAnyFrame::Error(f) => CanRawFrame::Classic(*f.as_ref()),
    }
}
#[test]
fn mature_native_frames_preserve_dm_flags_padding_and_caller_time() {
    let cases = [
        frame(0x123, 0, None, b"12345678", 17),
        frame(0x12, EXTENDED | RTR, Some(4), b"ABCD", 18),
        frame(0x456, FD | BRS, None, (0u8..64).collect::<Vec<_>>(), 99),
        frame(2, FD, Some(9), b"a", 100),
    ];
    let native_headers = [
        (0x123, 8, None),
        (CAN_EFF_FLAG | CAN_RTR_FLAG | 0x12, 4, None),
        (0x456, 64, Some(5)),
        (2, 12, Some(4)),
    ];
    for (f, expected_header) in cases.into_iter().zip(native_headers) {
        let native = raw(encode(&f).unwrap());
        let header = match native {
            CanRawFrame::Classic(r) => (r.can_id, r.can_dlc, None),
            CanRawFrame::Fd(r) => (r.can_id, r.len, Some(r.flags)),
        };
        assert_eq!(header, expected_header);
        let decoded = decode(native, 700).unwrap();
        let mut data = f.data().to_vec();
        data.resize(f.length(), 0);
        assert_eq!(
            decoded,
            frame(f.can_id(), f.flags(), Some(f.dlc()), data, 700)
        );
    }
    let encoded = encode(&frame(0x123, 0, None, b"abc", 7)).unwrap();
    // Linux UAPI can_frame: native-endian can_id, len, reserved[3], data[8].
    let mut expected = vec![];
    expected.extend(0x123u32.to_ne_bytes());
    expected.extend([3, 0, 0, 0, b'a', b'b', b'c', 0, 0, 0, 0, 0]);
    assert_eq!(unsafe { encoded.as_bytes() }, expected);
}
#[test]
fn raw_admission_precedes_upstream_length_normalization() {
    for (id, length) in [(CAN_ERR_FLAG | 1, 8), (0x1000, 1), (1, 9)] {
        let mut r = can_frame_default();
        r.can_id = id;
        r.can_dlc = length;
        assert!(decode(r.into(), 0).is_err());
    }
    for (id, length, flags) in [
        (1, 1, 0),
        (1, 1, CANFD_FDF | CANFD_ESI),
        (1, 1, CANFD_FDF | 8),
        (CAN_RTR_FLAG | 1, 1, CANFD_FDF),
        (CAN_EFF_FLAG | 1, 9, CANFD_FDF),
        (1, 65, CANFD_FDF),
    ] {
        let mut r = canfd_frame_default();
        r.can_id = id;
        r.len = length;
        r.flags = flags as u8;
        assert!(decode(r.into(), 0).is_err());
    }
    let mut r = can_frame_default();
    r.can_id = 1;
    r.can_dlc = 1;
    r.len8_dlc = 9;
    assert!(decode(r.into(), 0).is_err());
}
#[derive(Default)]
struct Io {
    read: VecDeque<io::Result<CanRawFrame>>,
    writes: Vec<CanAnyFrame>,
    blocked: bool,
    short: bool,
    attempts: usize,
}
impl CanDatagramIo for Io {
    fn receive(&mut self) -> io::Result<CanRawFrame> {
        self.read
            .pop_front()
            .unwrap_or_else(|| Err(io::ErrorKind::WouldBlock.into()))
    }
    fn send(&mut self, frame: &CanAnyFrame) -> io::Result<usize> {
        self.attempts += 1;
        if self.blocked {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        self.writes.push(*frame);
        Ok(frame.size() - usize::from(self.short))
    }
}
#[test]
fn bounded_datagrams_keep_complete_queue_on_backpressure_without_partial_retries() {
    let mut t = SocketCanTransport::new(
        Io {
            blocked: true,
            ..Default::default()
        },
        true,
        2,
        1,
    )
    .unwrap();
    let f = frame(1, 0, None, b"a", 0);
    t.send_frame(&f).unwrap();
    t.send_frame(&f).unwrap();
    assert_eq!(t.pending(), 2);
    assert!(t.send_frame(&f).unwrap_err().to_string().contains("full"));
    assert!(t.failure().is_none());
    t.io_mut().blocked = false;
    assert!(!t.flush().unwrap());
    assert_eq!(t.pending(), 1);
    assert!(t.flush().unwrap());
    assert_eq!(t.io().writes.len(), 2);
    t.io_mut().short = true;
    let failure = t.send_frame(&f).unwrap_err();
    let attempts = t.io().attempts;
    assert!(Arc::ptr_eq(&failure, &t.flush().unwrap_err()));
    assert_eq!(t.io().attempts, attempts);
    assert_eq!(t.pending(), 1);
}
#[test]
fn receive_is_one_bounded_record_and_bad_head_freezes_physical_suffix() {
    let f = frame(1, 0, None, b"x", 0);
    let mut bad = can_frame_default();
    bad.can_dlc = 9;
    let input = VecDeque::from([
        Ok(raw(encode(&f).unwrap())),
        Ok(bad.into()),
        Ok(raw(encode(&f).unwrap())),
    ]);
    let mut t = SocketCanTransport::new(
        Io {
            read: input,
            ..Default::default()
        },
        true,
        4,
        1,
    )
    .unwrap();
    assert_eq!(t.receive_once(500).unwrap(), Some(f.with_timestamp(500)));
    assert_eq!(t.io().read.len(), 2);
    let failure = t.receive_once(501).unwrap_err();
    assert!(Arc::ptr_eq(&failure, &t.receive_once(502).unwrap_err()));
    assert_eq!(t.io().read.len(), 1);
    let mut classic = SocketCanTransport::new(Io::default(), false, 1, 1).unwrap();
    assert!(classic.send_frame(&frame(1, FD, None, b"x", 0)).is_err());
    assert_eq!(classic.io().attempts, 0);
}

use sim_core::{Endpoint, EventPayload, LinkSpec, NodeSpec, SimulationOptions, Topology};
use sim_host::{
    endpoints::socketcan::{SocketCan, SocketCanOptions},
    runtime::{registry, CanHost, HostEvent, HostOptions, HostSimulation},
    transports::socketcan::NativeCanSocket,
};
use socketcan::{CanFdSocket, Socket};
fn interface() -> String {
    std::env::var("SIM_SOCKETCAN_IFACE")
        .expect("run in an isolated namespace with SIM_SOCKETCAN_IFACE set")
}
#[test]
#[ignore = "requires a real Linux vcan/CAN interface; socket fixtures do not establish this gate"]
fn actual_vcan_datagrams_preserve_classic_rtr_extended_fd_and_brs() {
    let interface = interface();
    let peer = CanFdSocket::open(&interface).unwrap();
    peer.set_read_timeout(std::time::Duration::from_millis(200))
        .unwrap();
    let mut transport =
        SocketCanTransport::new(NativeCanSocket::open(&interface, true).unwrap(), true, 4, 4)
            .unwrap();
    let cases = [
        frame(0x123, 0, None, b"classic!", 999),
        frame(0x12, EXTENDED | RTR, Some(4), b"ABCD", 999),
        frame(0x456, FD | BRS, None, (0u8..64).collect::<Vec<_>>(), 999),
    ];
    for expected in &cases {
        transport.send_frame(expected).unwrap();
        assert_eq!(
            decode(peer.read_raw_frame().unwrap(), 77).unwrap(),
            expected.with_timestamp(77)
        );
    }
    for expected in &cases {
        peer.write_frame(&encode(expected).unwrap()).unwrap();
        assert_eq!(
            transport.receive_once(88).unwrap(),
            Some(expected.with_timestamp(88))
        );
    }
    assert_eq!(transport.pending(), 0);
    assert!(transport.receive_once(99).unwrap().is_none());
    // Separate classic acquisition exercises enable_fd=false at the real kernel boundary.
    let mut classic = SocketCanTransport::new(
        NativeCanSocket::open(&interface, false).unwrap(),
        false,
        1,
        1,
    )
    .unwrap();
    classic.send_frame(&cases[0]).unwrap();
    assert_eq!(
        decode(peer.read_raw_frame().unwrap(), 77).unwrap(),
        cases[0].with_timestamp(77)
    );
    println!("PASS actual Linux vcan classic/RTR/EFF/FD/BRS, sampled timestamps77/88; no QEMU/physical arbitration claim");
}
fn link(a: &str, b: &str) -> LinkSpec {
    LinkSpec::new(Endpoint::parse(a).unwrap(), Endpoint::parse(b).unwrap())
}
#[test]
#[ignore = "requires a real Linux vcan/CAN interface; no mock substitutes for this consumer"]
fn actual_simulation_socketcan_fabric_is_bidirectional_bounded_and_releases_fd() {
    let interface = interface();
    let peer = CanFdSocket::open(&interface).unwrap();
    peer.set_read_timeout(std::time::Duration::from_millis(200))
        .unwrap();
    let mut options = SocketCanOptions::new(interface.clone());
    options.io_budget = 1;
    let topology = Topology::new(
        vec![
            NodeSpec::new("bus", "bus.can", HostOptions::CanBus(Default::default())),
            NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
            NodeSpec::new("kernel", "host.socketcan", HostOptions::SocketCan(options)),
        ],
        vec![link("host.can", "bus.can"), link("kernel.can", "bus.can")],
    )
    .unwrap();
    let mut sim = HostSimulation::new(
        topology,
        registry().unwrap(),
        SimulationOptions {
            quantum_ns: 10,
            ..Default::default()
        },
    )
    .unwrap();
    sim.start(None).unwrap();
    let incoming = [
        frame(0x321, FD | BRS, None, (0u8..12).collect::<Vec<_>>(), 500),
        frame(0x122, EXTENDED | RTR, Some(3), b"abc", 501),
    ];
    for f in &incoming {
        peer.write_frame(&encode(f).unwrap()).unwrap();
    }
    sim.step(None, None).unwrap();
    let observed = |sim: &HostSimulation| {
        sim.events()
            .into_iter()
            .filter_map(|e| match e.payload {
                EventPayload::Observation(HostEvent::CanHostReceived { frame }) => Some(frame),
                _ => None,
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(observed(&sim), [incoming[0].with_timestamp(10)]);
    sim.step(None, None).unwrap();
    assert_eq!(
        observed(&sim),
        [
            incoming[0].with_timestamp(10),
            incoming[1].with_timestamp(20)
        ]
    );
    let host = sim.handle::<CanHost>("host").unwrap();
    let outgoing = frame(0x7ff, 0, None, b"out", 123);
    sim.with_component(&host, |host, _| host.send(outgoing.clone()))
        .unwrap();
    sim.run_until(20, None).unwrap();
    assert_eq!(
        decode(peer.read_raw_frame().unwrap(), 1000).unwrap(),
        outgoing.with_timestamp(1000)
    );
    let component = sim.handle::<SocketCan>("kernel").unwrap();
    assert_eq!(
        sim.with_component(&component, |can, _| Ok(can.pending()))
            .unwrap(),
        0
    );
    let fd_count = || {
        std::fs::read_dir("/proc/self/fd")
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                std::fs::read_link(entry.path())
                    .is_ok_and(|path| path.to_string_lossy().starts_with("socket:"))
            })
            .count()
    };
    let before = fd_count();
    sim.close().unwrap();
    assert_eq!(fd_count() + 1, before);
    println!("PASS actual Simulation->SocketCAN->vcan and vcan->CAN fabric->consumer, io_budget1, cuts10/20, owned fd closed");
}
#[test]
fn pure_interface_admission_and_unavailable_interface_are_explicit() {
    for name in ["", "sixteen-byte-name!", "bad/name", "bad\0name"] {
        assert!(SocketCanOptions::new(name).validate().is_err());
    }
    assert!(NativeCanSocket::open("nosimcan000000", true)
        .err()
        .unwrap()
        .to_string()
        .contains("No such device"));
}
