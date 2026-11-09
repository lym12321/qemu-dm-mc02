//! Borrowed transport tests and a real Unix socketpair -> fixed84 endpoint ->
//! sole CAN queue -> endpoint -> socket consumer. No QEMU CPU or silicon claim.

use sim_host::{
    can::{CanBus, CanBusOptions, CanCallbacks, CanFrame, EndpointId, BRS, FD},
    endpoints::qemu_can::{QemuCanEndpoint, QemuTimestampMapper},
    transports::{
        fixed84::{CanFrameSink, CanStream},
        stream::StreamTransport,
    },
    Error, Failure, Result,
};
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    io::{self, Read, Write},
    rc::Rc,
    sync::Arc,
};

enum WriteAction {
    Limit(usize),
    Block,
    Fail,
    Zero,
}

#[derive(Default)]
struct ScriptedIo {
    writes: Rc<RefCell<VecDeque<WriteAction>>>,
    output: Rc<RefCell<Vec<u8>>>,
    input: VecDeque<Vec<u8>>,
    reads: Rc<Cell<usize>>,
}

impl Write for ScriptedIo {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let length = match self.writes.borrow_mut().pop_front() {
            Some(WriteAction::Limit(length)) => length.min(bytes.len()),
            Some(WriteAction::Block) => return Err(io::ErrorKind::WouldBlock.into()),
            Some(WriteAction::Fail) => {
                return Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "scripted failure",
                ))
            }
            Some(WriteAction::Zero) => return Ok(0),
            None => bytes.len(),
        };
        self.output.borrow_mut().extend_from_slice(&bytes[..length]);
        Ok(length)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl Read for ScriptedIo {
    fn read(&mut self, target: &mut [u8]) -> io::Result<usize> {
        assert_eq!(target.len(), 4096);
        self.reads.set(self.reads.get() + 1);
        let bytes = self.input.pop_front().ok_or(io::ErrorKind::WouldBlock)?;
        target[..bytes.len()].copy_from_slice(&bytes);
        Ok(bytes.len())
    }
}

#[test]
fn partial_writes_keep_suffix_order_and_capacity_is_retryable() {
    let mut io = ScriptedIo::default();
    let plan = io.writes.clone();
    let output = io.output.clone();
    plan.borrow_mut().extend([
        WriteAction::Limit(2),
        WriteAction::Block,
        WriteAction::Block,
    ]);
    let mut stream = StreamTransport::new(&mut io, 2).unwrap();
    stream.send(b"abcdef".to_vec()).unwrap();
    stream.send(b"ghij".to_vec()).unwrap();
    assert!(matches!(
        &*stream.send(b"never".to_vec()).unwrap_err(),
        Error::Capacity(_)
    ));
    assert_eq!(output.borrow().as_slice(), b"ab");
    assert_eq!(stream.pending_records(), 2);
    assert!(stream.flush().unwrap());
    stream.send(b"retry".to_vec()).unwrap();
    assert_eq!(output.borrow().as_slice(), b"abcdefghijretry");
}

#[test]
fn physical_read_fragments_survive_until_complete_with_one_read_per_call() {
    let mut io = ScriptedIo::default();
    io.input.extend([b"abcde".to_vec(), b"f".to_vec(), vec![]]);
    let reads = io.reads.clone();
    let mut stream = StreamTransport::new(&mut io, 1).unwrap();
    assert!(stream.next_record(0).is_err());
    stream.read_once().unwrap();
    assert_eq!(stream.next_record(3).unwrap(), Some(b"abc".to_vec()));
    assert_eq!(stream.next_record(3).unwrap(), None);
    assert_eq!(reads.get(), 1);
    stream.read_once().unwrap();
    assert_eq!(stream.next_record(3).unwrap(), Some(b"def".to_vec()));
    assert_eq!(reads.get(), 2);
    assert!(
        matches!(&*stream.read_once().unwrap_err(), Error::Io(error) if error.kind() == io::ErrorKind::UnexpectedEof)
    );
}

#[test]
fn length_prefix_admission_keeps_partial_invalid_and_later_records_in_one_buffer() {
    let mut io = ScriptedIo::default();
    io.input.extend([
        vec![3, 0],
        vec![0, 0, b'o'],
        b"ne\x03\0\0\0two\xff\xff\xff\xff".to_vec(),
    ]);
    let reads = io.reads.clone();
    let mut stream = StreamTransport::new(&mut io, 1).unwrap();
    for expected_bytes in [2, 5] {
        stream.read_once().unwrap();
        assert!(stream.next_length_prefixed(1, 8).unwrap().is_none());
        assert_eq!(stream.buffered_bytes(), expected_bytes);
    }
    stream.read_once().unwrap();
    assert_eq!(
        stream.next_length_prefixed(1, 8).unwrap(),
        Some(b"one".to_vec())
    );
    assert_eq!(
        stream.next_length_prefixed(1, 8).unwrap(),
        Some(b"two".to_vec())
    );
    assert!(stream.next_length_prefixed(1, 8).is_err());
    assert!(stream.next_length_prefixed(1, 8).is_err());
    assert_eq!((stream.buffered_bytes(), reads.get()), (4, 3));
    assert!(stream.next_length_prefixed(9, 8).is_err());
    if usize::BITS > 32 {
        assert!(stream
            .next_length_prefixed(0, u32::MAX as usize + 1)
            .is_err());
    }
}

#[test]
fn length_prefixed_send_admits_before_flushing_and_uses_one_record() {
    let mut io = ScriptedIo::default();
    io.writes.borrow_mut().push_back(WriteAction::Block);
    let output = io.output.clone();
    let mut stream = StreamTransport::new(&mut io, 1).unwrap();
    stream.send(b"old".to_vec()).unwrap();
    assert!(stream.send_length_prefixed(b"", 1, 8).is_err());
    assert!(stream.send_length_prefixed(b"one", 9, 8).is_err());
    assert!(output.borrow().is_empty());
    assert_eq!(stream.pending_records(), 1);
    stream.send_length_prefixed(b"one", 1, 8).unwrap();
    assert_eq!(output.borrow().as_slice(), b"old\x03\0\0\0one");
}

#[test]
fn failed_and_zero_writes_keep_pending_record_without_inventing_success() {
    for action in [WriteAction::Fail, WriteAction::Zero] {
        let mut io = ScriptedIo::default();
        io.writes.borrow_mut().push_back(action);
        let mut stream = StreamTransport::new(&mut io, 1).unwrap();
        assert!(matches!(
            &*stream.send(b"pending".to_vec()).unwrap_err(),
            Error::Io(_)
        ));
        assert_eq!(stream.pending_records(), 1);
    }
    assert!(StreamTransport::new(&mut ScriptedIo::default(), 0).is_err());
}

fn wire(id: u32, flags: u32, dlc: u8, timestamp: u64, payload: &[u8]) -> Vec<u8> {
    // Independent public format expression: no production encoder is called.
    let mut bytes = Vec::new();
    bytes.extend(id.to_le_bytes());
    bytes.extend(flags.to_le_bytes());
    bytes.extend([dlc, 0, 0, 0]);
    bytes.extend(timestamp.to_le_bytes());
    bytes.extend(payload);
    bytes.resize(84, 0);
    bytes
}

fn frame(id: u32, data: &[u8], timestamp: u64) -> CanFrame {
    CanFrame::new(id, 0, None, data.to_vec(), timestamp).unwrap()
}

#[test]
fn invalid_fixed84_head_does_not_consume_a_later_buffered_record() {
    let mut bad = wire(1, 0, 0, 0, &[]);
    bad[9] = 1;
    bad.extend(wire(2, 0, 2, 5, b"ok"));
    let mut io = ScriptedIo::default();
    io.input.push_back(bad);
    let reads = io.reads.clone();
    let mut stream = CanStream::new(&mut io, 1).unwrap();
    assert!(stream.receive().is_err());
    assert_eq!(stream.transport().buffered_bytes(), 84);
    assert_eq!(reads.get(), 1);
    assert_eq!(stream.receive().unwrap(), vec![frame(2, b"ok", 5)]);
}

#[derive(Default)]
struct EffectThenFailure {
    frames: Vec<CanFrame>,
    error: Option<Failure>,
}

impl CanFrameSink for EffectThenFailure {
    fn send_frame(&mut self, frame: &CanFrame) -> Result<()> {
        self.frames.push(frame.clone());
        match &self.error {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}

struct Router<'a, 'b, T: CanFrameSink> {
    endpoint: &'a mut QemuCanEndpoint<'b, T>,
    received: Vec<CanFrame>,
}

impl<T: CanFrameSink> CanCallbacks for Router<'_, '_, T> {
    fn receive(
        &mut self,
        destination: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> Result<()> {
        if destination == self.endpoint.id() {
            self.endpoint.receive(frame)
        } else {
            self.received.push(frame.clone());
            Ok(())
        }
    }
}

#[test]
fn endpoint_requires_mapping_and_checked_epoch_arithmetic_precedes_io() {
    let mut sink = EffectThenFailure::default();
    let mut bus = CanBus::new(Default::default()).unwrap();
    {
        let mut endpoint = QemuCanEndpoint::new(&mut bus, &mut sink, "qemu").unwrap();
        assert!(endpoint.ingest(&mut bus, &frame(1, &[], 1), 0).is_err());
        assert!(endpoint.receive(&frame(1, &[], 1)).is_err());
        endpoint.bind_mapper(QemuTimestampMapper {
            qemu_origin_ns: 10,
            worker_origin_ns: u64::MAX,
        });
        assert!(endpoint.ingest(&mut bus, &frame(1, &[], 11), 0).is_err());
        let mut foreign_bus = CanBus::new(Default::default()).unwrap();
        foreign_bus
            .attach(endpoint.id(), "same endpoint token")
            .unwrap();
        assert!(endpoint
            .ingest(&mut foreign_bus, &frame(1, &[], 10), 0)
            .is_err());
        assert_eq!(foreign_bus.pending(), 0);
        endpoint.bind_mapper(QemuTimestampMapper {
            qemu_origin_ns: u64::MAX,
            worker_origin_ns: 0,
        });
        assert!(endpoint.receive(&frame(1, &[], 1)).is_err());
        assert_eq!(endpoint.submitted_frames(), 0);
        assert!(bus.failure().is_none());
        assert_eq!(bus.pending(), 0);
    }
    assert!(sink.frames.is_empty());
    let mapper = QemuTimestampMapper {
        qemu_origin_ns: 9000,
        worker_origin_ns: 100,
    };
    assert_eq!(mapper.map(8999, 123).unwrap(), 123);
    assert_eq!(mapper.unmap(99, 9000).unwrap(), 9000);
}

#[test]
fn partial_effect_then_error_freezes_bus_without_counting_or_replaying_submission() {
    let primary = Arc::new(Error::Io(io::Error::new(
        io::ErrorKind::BrokenPipe,
        "after physical write",
    )));
    let mut sink = EffectThenFailure {
        error: Some(primary.clone()),
        ..Default::default()
    };
    let mut bus = CanBus::new(Default::default()).unwrap();
    {
        let mut endpoint = QemuCanEndpoint::new(&mut bus, &mut sink, "qemu").unwrap();
        endpoint.bind_mapper(QemuTimestampMapper {
            qemu_origin_ns: 1000,
            worker_origin_ns: 0,
        });
        let source = EndpointId::new();
        bus.attach(&source, "device").unwrap();
        bus.send(&source, frame(0x201, b"state", 10), None).unwrap();
        let mut router = Router {
            endpoint: &mut endpoint,
            received: vec![],
        };
        assert!(Arc::ptr_eq(
            &primary,
            &bus.step(10, None, &mut router).unwrap_err()
        ));
        assert_eq!(router.endpoint.submitted_frames(), 0);
        assert!(Arc::ptr_eq(
            &primary,
            &bus.step(10, None, &mut router).unwrap_err()
        ));
        assert_eq!((bus.pending(), bus.time_ns()), (0, 10));
        bus.close().unwrap();
    }
    assert_eq!(sink.frames, vec![frame(0x201, b"state", 1010)]);
}

#[cfg(unix)]
#[test]
fn actual_socketpair_ingress_and_reset_epoch_egress_preserve_borrowed_lifetime() {
    use std::os::unix::net::UnixStream;
    let (mut socket, mut peer) = UnixStream::pair().unwrap();
    socket.set_nonblocking(true).unwrap();
    peer.set_nonblocking(true).unwrap();
    {
        let mut stream = CanStream::new(&mut socket, 256).unwrap();
        let mut bus = CanBus::new(Default::default()).unwrap();
        let mut endpoint = QemuCanEndpoint::new(&mut bus, &mut stream, "qemu").unwrap();
        let device = EndpointId::new();
        bus.attach(&device, "device").unwrap();
        bus.reset(100).unwrap();
        endpoint.bind_mapper(QemuTimestampMapper {
            qemu_origin_ns: 9000,
            worker_origin_ns: 100,
        });
        for (flags, dlc, data) in [
            (0, 8, (0..8).collect::<Vec<u8>>()),
            (FD | BRS, 15, (0..64).collect()),
        ] {
            let bytes = wire(0x310, flags, dlc, 9005, &data);
            peer.write_all(&bytes[..19]).unwrap();
            assert!(endpoint.stream_mut().receive().unwrap().is_empty());
            // Logical reset and mapper rebinding leave the stream's prefix.
            bus.reset(100).unwrap();
            endpoint.bind_mapper(QemuTimestampMapper {
                qemu_origin_ns: 9000,
                worker_origin_ns: 100,
            });
            peer.write_all(&bytes[19..]).unwrap();
            let incoming = endpoint.stream_mut().receive().unwrap();
            assert_eq!(incoming.len(), 1);
            endpoint.ingest(&mut bus, &incoming[0], 100).unwrap();
            assert_eq!((bus.time_ns(), bus.pending()), (100, 1));
            let mut router = Router {
                endpoint: &mut endpoint,
                received: vec![],
            };
            assert_eq!(bus.step(104, None, &mut router).unwrap(), 0);
            assert_eq!(bus.step(105, None, &mut router).unwrap(), 1);
            assert_eq!(
                router.received,
                vec![CanFrame::new(0x310, flags, Some(dlc), data, 105).unwrap()]
            );
        }
        // A stale QEMU epoch falls back to the supplied current cut.
        endpoint
            .ingest(&mut bus, &frame(0x311, b"late", 8999), 105)
            .unwrap();
        let mut router = Router {
            endpoint: &mut endpoint,
            received: vec![],
        };
        bus.step(105, None, &mut router).unwrap();
        assert_eq!(router.received, vec![frame(0x311, b"late", 105)]);
        bus.send(&device, frame(0x201, b"state", 105), None)
            .unwrap();
        bus.step(105, None, &mut router).unwrap();
        assert_eq!(router.endpoint.submitted_frames(), 1);
        let mut response = [0; 84];
        peer.read_exact(&mut response).unwrap();
        assert_eq!(response.as_slice(), wire(0x201, 0, 5, 9005, b"state"));
        bus.close().unwrap();
    }
    peer.write_all(b"still borrowed").unwrap();
    let mut response = [0; 14];
    socket.read_exact(&mut response).unwrap();
    assert_eq!(&response, b"still borrowed");
}

#[cfg(unix)]
#[test]
fn actual_backpressure_preserves_exact_old_epoch_suffix_and_submission_count() {
    use std::os::unix::net::UnixStream;
    let (mut socket, mut peer) = UnixStream::pair().unwrap();
    socket.set_nonblocking(true).unwrap();
    peer.set_nonblocking(true).unwrap();
    let mut stream = CanStream::new(&mut socket, 1).unwrap();
    let mut bus = CanBus::new(CanBusOptions::default()).unwrap();
    let mut endpoint = QemuCanEndpoint::new(&mut bus, &mut stream, "qemu").unwrap();
    endpoint.bind_mapper(QemuTimestampMapper {
        qemu_origin_ns: 1000,
        worker_origin_ns: 0,
    });
    let feedback = frame(0x201, b"state", 10);
    for _ in 0..4096 {
        endpoint.receive(&feedback).unwrap();
        if endpoint.stream().transport().tx_pending() {
            break;
        }
    }
    assert!(
        endpoint.stream().transport().tx_pending(),
        "finite prefill must reach actual kernel backpressure"
    );
    let count = endpoint.submitted_frames();
    assert!(matches!(
        &*endpoint.receive(&feedback).unwrap_err(),
        Error::Capacity(_)
    ));
    assert_eq!(endpoint.submitted_frames(), count);
    endpoint.bind_mapper(QemuTimestampMapper {
        qemu_origin_ns: 2000,
        worker_origin_ns: 0,
    });
    bus.reset(50).unwrap();
    let expected = wire(0x201, 0, 5, 1010, b"state").repeat(count as usize);
    let mut received = Vec::new();
    for _ in 0..4096 {
        let mut chunk = [0; 4096];
        match peer.read(&mut chunk) {
            Ok(length) => received.extend_from_slice(&chunk[..length]),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => (),
            Err(error) => panic!("{error}"),
        }
        endpoint.stream_mut().transport_mut().flush().unwrap();
        if received.len() == expected.len() {
            break;
        }
    }
    assert_eq!(received, expected);
    assert!(!endpoint.stream().transport().tx_pending());
    assert_eq!(endpoint.submitted_frames(), count);
}
