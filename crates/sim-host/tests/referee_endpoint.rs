#![cfg(all(unix, feature = "referee"))]
#[path = "support/referee.rs"]
mod support;
use sim_core::{Failure, Result};
use sim_host::{devices::referee::RefereeUartEndpoint, transports::stream::StreamTransport};
use std::{
    cell::{Cell, RefCell},
    io::{self, Read, Write},
    os::unix::net::UnixStream,
    rc::Rc,
    sync::Arc,
};
use support::state;
pub const WIRE: &str = concat!(
    "a50b00fe690100010000080706050403020138e1",
    "a50d00ffe6010203026400c80003006400500007ee23",
    "a50e0000370202000000000000000028000a00140071bd",
    "a5080001b8040200000000000000806ba9"
);
pub fn hex(value: &str) -> Vec<u8> {
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|v| u8::from_str_radix(std::str::from_utf8(v).unwrap(), 16).unwrap())
        .collect()
}

struct BudgetIo {
    socket: UnixStream,
    budget: Rc<Cell<usize>>,
}
impl Read for BudgetIo {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.socket.read(buffer)
    }
}
impl Write for BudgetIo {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let size = bytes.len().min(self.budget.get());
        if size == 0 {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let count = self.socket.write(&bytes[..size])?;
        self.budget.set(self.budget.get() - count);
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn receive(peer: &mut UnixStream) -> Vec<u8> {
    let mut bytes = vec![];
    let mut buffer = [0; 4096];
    loop {
        match peer.read(&mut buffer) {
            Ok(0) => panic!("unexpected EOF"),
            Ok(n) => bytes.extend_from_slice(&buffer[..n]),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
            Err(error) => panic!("{error}"),
        }
    }
    bytes
}
type Pair = (
    Rc<RefCell<StreamTransport<BudgetIo>>>,
    Rc<Cell<usize>>,
    UnixStream,
);
fn pair(budget: usize) -> Pair {
    let (sender, peer) = UnixStream::pair().unwrap();
    sender.set_nonblocking(true).unwrap();
    peer.set_nonblocking(true).unwrap();
    let budget = Rc::new(Cell::new(budget));
    (
        Rc::new(RefCell::new(
            StreamTransport::new(
                BudgetIo {
                    socket: sender,
                    budget: budget.clone(),
                },
                1,
            )
            .unwrap(),
        )),
        budget,
        peer,
    )
}
#[test]
fn actual_socket_retains_exact_partial_suffix_across_reset_and_close() {
    let (stream, budget, mut peer) = pair(19);
    let sender = stream.clone();
    let mut endpoint = RefereeUartEndpoint::new(
        move |bytes| {
            sender
                .borrow_mut()
                .send(bytes)
                .map_err(|error| error as Failure)
        },
        254,
    );
    let wire = endpoint.send_state(&state()).unwrap();
    assert_eq!(wire, hex(WIRE));
    assert_eq!(wire.len(), 82);
    assert_eq!(endpoint.next_sequence(), 2);
    assert_eq!(receive(&mut peer), wire[..19]);
    endpoint.reset(100).unwrap();
    endpoint.close();
    assert_eq!(stream.borrow().pending_records(), 1);
    assert!(receive(&mut peer).is_empty());
    budget.set(1000);
    assert!(stream.borrow_mut().flush().unwrap());
    assert_eq!(receive(&mut peer), wire[19..]);
    stream
        .borrow_mut()
        .send(b"still-borrowed".to_vec())
        .unwrap();
    assert_eq!(receive(&mut peer), b"still-borrowed");
}
#[test]
fn capacity_retry_and_full_snapshot_admission_preserve_sequence_and_older_queue() {
    let (stream, budget, mut peer) = pair(0);
    stream.borrow_mut().send(b"older".to_vec()).unwrap();
    let sender = stream.clone();
    let mut endpoint = RefereeUartEndpoint::new(
        move |bytes| {
            sender
                .borrow_mut()
                .send(bytes)
                .map_err(|error| error as Failure)
        },
        254,
    );
    assert!(endpoint
        .send_state(&state())
        .unwrap_err()
        .to_string()
        .contains("queue"));
    assert_eq!(endpoint.next_sequence(), 254);
    budget.set(1000);
    let mut invalid = state();
    invalid.robot.power_outputs = 8;
    assert!(endpoint.send_state(&invalid).is_err());
    assert!(receive(&mut peer).is_empty());
    assert_eq!(stream.borrow().pending_records(), 1);
    assert_eq!(endpoint.send_state(&state()).unwrap(), hex(WIRE));
    assert_eq!(receive(&mut peer), [b"older".to_vec(), hex(WIRE)].concat());
}
#[test]
fn other_output_failure_retains_original_identity_and_requires_reset() {
    let error: Failure = Arc::new(io::Error::other("partial output failure"));
    let calls = Rc::new(Cell::new(0));
    let failure = error.clone();
    let count = calls.clone();
    let mut endpoint = RefereeUartEndpoint::new(
        move |_| -> Result<()> {
            count.set(count.get() + 1);
            Err(failure.clone())
        },
        254,
    );
    assert!(Arc::ptr_eq(
        &error,
        &endpoint.send_state(&state()).unwrap_err()
    ));
    assert!(Arc::ptr_eq(
        &error,
        &endpoint.send_state(&state()).unwrap_err()
    ));
    assert_eq!(calls.get(), 1);
    assert_eq!(endpoint.next_sequence(), 254);
    endpoint.reset(1).unwrap();
    assert!(endpoint.send_state(&state()).is_err());
    assert_eq!(calls.get(), 2);
}
