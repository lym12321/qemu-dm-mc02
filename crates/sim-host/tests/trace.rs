#![cfg(feature = "trace")]
use binrw::{BinWrite, BinWriterExt};
use mcap::records;
use sim_host::trace::*;
use std::{
    cell::RefCell,
    collections::BTreeMap,
    io::{self, Cursor, Read, Write},
    path::PathBuf,
    process::Command,
    rc::Rc,
    sync::Arc,
};

fn event(timestamp: u64, source: &str, payload: impl Into<Vec<u8>>, clock: &str) -> TraceEvent {
    TraceEvent::new(timestamp, source, "fdcan1", "can.tx", payload, clock).unwrap()
}
fn events() -> Vec<TraceEvent> {
    vec![
        event(900, "east", [0, 255, 65], "qemu:east:reset0"),
        event(3, "west", b"west", "qemu:west:reset0"),
        event(0, "east", [], "qemu:east:reset1"),
    ]
}
fn written(events: &[TraceEvent]) -> Vec<u8> {
    let mut output = vec![];
    {
        let mut writer = TraceWriter::new(&mut output).unwrap();
        for event in events {
            writer.append(event).unwrap();
        }
        writer.finish().unwrap();
    }
    output
}
fn encoded<T: for<'a> BinWrite<Args<'a> = ()>>(value: &T) -> Vec<u8> {
    let mut output = Cursor::new(vec![]);
    output.write_le(value).unwrap();
    output.into_inner()
}
fn record(opcode: u8, body: &[u8]) -> Vec<u8> {
    let mut out = vec![opcode];
    out.extend((body.len() as u64).to_le_bytes());
    out.extend(body);
    out
}
fn header() -> Vec<u8> {
    record(
        1,
        &encoded(&records::Header {
            profile: PROFILE.into(),
            library: "independent".into(),
        }),
    )
}
fn channel() -> Vec<u8> {
    record(
        4,
        &encoded(&records::Channel {
            id: 1,
            schema_id: 0,
            topic: "fdcan1".into(),
            message_encoding: ENCODING.into(),
            metadata: BTreeMap::from([
                ("source".into(), "east".into()),
                ("event_type".into(), "can.tx".into()),
                ("clock".into(), "qemu:east:reset0".into()),
            ]),
        }),
    )
}
fn message(sequence: u32, publish_time: u64) -> Vec<u8> {
    let mut bytes = encoded(&records::MessageHeader {
        channel_id: 1,
        sequence,
        log_time: 23,
        publish_time,
    });
    bytes.extend([0, 255, 65]);
    record(5, &bytes)
}
fn container(records: impl IntoIterator<Item = Vec<u8>>) -> Vec<u8> {
    let mut raw = mcap::MAGIC.to_vec();
    for record in records {
        raw.extend(record);
    }
    raw.extend(record(15, &[0; 4]));
    raw.extend(record(2, &[0; 20]));
    raw.extend(mcap::MAGIC);
    raw
}
fn read(raw: &[u8]) -> TraceResult<Vec<TraceEvent>> {
    iter_events(&mut Cursor::new(raw)).collect()
}

#[derive(Default)]
struct SinkState {
    bytes: Vec<u8>,
    writes: usize,
    flushes: usize,
    failure: Option<&'static str>,
}
struct Sink(Rc<RefCell<SinkState>>);
impl Write for Sink {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let mut state = self.0.borrow_mut();
        state.writes += 1;
        if state.failure == Some("io") {
            return Err(io::Error::other("injected output failure"));
        }
        let count = if state.failure == Some("short") {
            bytes.len() - 1
        } else {
            bytes.len()
        };
        state.bytes.extend(&bytes[..count]);
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        let mut state = self.0.borrow_mut();
        state.flushes += 1;
        if state.failure == Some("flush") {
            Err(io::Error::other("injected flush failure"))
        } else {
            Ok(())
        }
    }
}

#[test]
fn writer_preserves_physical_order_epochs_and_explicit_finish() {
    let expected = events();
    let raw = written(&expected);
    assert_eq!(read(&raw).unwrap(), expected);
    assert_eq!(
        read(include_bytes!("fixtures/python_trace_v1.mcap")).unwrap(),
        expected
    );
    let records: Vec<_> = mcap::read::LinearReader::new(&raw)
        .unwrap()
        .collect::<mcap::McapResult<_>>()
        .unwrap();
    assert_eq!(
        records.iter().map(|r| r.opcode()).collect::<Vec<_>>(),
        [1, 4, 5, 4, 5, 4, 5, 15, 2]
    );
    assert!(
        matches!(&records[0],records::Record::Header(header) if header.profile=="robot-sim/trace/1")
    );
    assert!(matches!(&records[7],records::Record::DataEnd(end) if end.data_section_crc!=0));
    assert!(
        matches!(&records[8],records::Record::Footer(footer) if *footer==records::Footer::default())
    );
    let state = Rc::new(RefCell::new(SinkState::default()));
    let mut output = Sink(state.clone());
    let mut writer = TraceWriter::new(&mut output).unwrap();
    writer.finish().unwrap();
    let before = (
        state.borrow().writes,
        state.borrow().flushes,
        state.borrow().bytes.len(),
    );
    writer.finish().unwrap();
    assert!(writer.append(&expected[0]).is_err());
    assert_eq!(
        (
            state.borrow().writes,
            state.borrow().flushes,
            state.borrow().bytes.len()
        ),
        before
    );
}

#[test]
fn bounds_reject_admission_without_writes_and_existing_channel_stays_usable() {
    let maximum = TraceEvent::new(
        u64::MAX,
        "é".repeat(127) + "a",
        "fdcan1",
        "can.tx",
        vec![0x5a; 65536],
        "clock",
    )
    .unwrap();
    assert_eq!(
        read(&written(std::slice::from_ref(&maximum))).unwrap(),
        [maximum]
    );
    for label in ["".into(), "bad\0name".into(), "é".repeat(128)] {
        assert!(TraceEvent::new(0, label, "p", "e", [], "c").is_err());
    }
    assert!(TraceEvent::new(0, "s", "p", "e", vec![0; 65537], "c").is_err());
    let state = Rc::new(RefCell::new(SinkState::default()));
    let mut output = Sink(state.clone());
    let mut writer = TraceWriter::new(&mut output).unwrap();
    for index in 0..256 {
        writer
            .append(&TraceEvent::new(0, "s", format!("p{index}"), "e", [], "c").unwrap())
            .unwrap();
    }
    let writes = state.borrow().writes;
    assert!(writer
        .append(&TraceEvent::new(0, "s", "overflow", "e", [], "c").unwrap())
        .is_err());
    assert_eq!(state.borrow().writes, writes);
    writer
        .append(&TraceEvent::new(0, "s", "p0", "e", b"existing", "c").unwrap())
        .unwrap();
    writer.finish().unwrap();
    drop(writer);
    assert_eq!(
        read(&state.borrow().bytes)
            .unwrap()
            .last()
            .unwrap()
            .payload(),
        b"existing"
    );
}

#[test]
fn partial_write_flush_and_io_errors_freeze_without_drop_retry() {
    for mode in ["short", "io", "flush"] {
        let state = Rc::new(RefCell::new(SinkState::default()));
        let mut output = Sink(state.clone());
        let mut writer = TraceWriter::new(&mut output).unwrap();
        writer.append(&events()[0]).unwrap();
        state.borrow_mut().failure = Some(mode);
        let error = if mode == "flush" {
            writer.finish().unwrap_err()
        } else {
            writer.append(&events()[1]).unwrap_err()
        };
        let stopped = (
            state.borrow().bytes.len(),
            state.borrow().writes,
            state.borrow().flushes,
        );
        state.borrow_mut().failure = None;
        assert!(Arc::ptr_eq(
            &writer.append(&events()[0]).unwrap_err(),
            &error
        ));
        assert!(Arc::ptr_eq(&writer.finish().unwrap_err(), &error));
        drop(writer);
        assert_eq!(
            (
                state.borrow().bytes.len(),
                state.borrow().writes,
                state.borrow().flushes
            ),
            stopped
        );
    }
    let mut output = vec![];
    {
        let mut writer = TraceWriter::new(&mut output).unwrap();
        writer.append(&events()[0]).unwrap();
    }
    let mut cursor = Cursor::new(output);
    let mut reader = iter_events(&mut cursor);
    assert!(reader.next().unwrap().is_ok());
    assert!(reader.next().unwrap().is_err());
    assert!(!reader.is_complete());
}

#[test]
fn footer_crc_magic_and_real_eof_require_exhaustion() {
    let expected = events();
    let valid = written(&expected);
    let mut corrupted = valid.clone();
    let payload = corrupted
        .windows(3)
        .position(|bytes| bytes == [0, 255, 65])
        .unwrap();
    corrupted[payload + 2] ^= 1;
    for invalid in [
        valid[..valid.len() - 1].to_vec(),
        valid[..valid.len() - 8].to_vec(),
        [valid.clone(), b"extra".to_vec()].concat(),
        [valid.clone(), valid.clone()].concat(),
        corrupted,
    ] {
        assert!(read(&invalid).is_err());
    }
    let mut truncated = Cursor::new(&valid[..valid.len() - 1]);
    let mut provisional = iter_events(&mut truncated);
    assert_eq!(provisional.next().unwrap().unwrap(), expected[0]);
    assert!(!provisional.is_complete());
    assert!(provisional.try_for_each(|event| event.map(|_| ())).is_err());
    assert!(!provisional.is_complete());
    assert!(provisional.next().is_none());
    assert!(read(&written(&[])).unwrap().is_empty());
    assert_eq!(
        read(&container([header(), channel(), message(0, 23)])).unwrap(),
        [event(23, "east", [0, 255, 65], "qemu:east:reset0")]
    );
}

#[test]
fn native_record_guards_reject_padding_map_length_duplicates_and_profile_order() {
    let mut padded = header();
    let len = u64::from_le_bytes(padded[1..9].try_into().unwrap());
    padded[1..9].copy_from_slice(&(len + 1).to_le_bytes());
    padded.push(0);
    let mut bad_map = channel();
    let prefix = encoded(&records::Channel {
        id: 1,
        schema_id: 0,
        topic: "fdcan1".into(),
        message_encoding: ENCODING.into(),
        metadata: BTreeMap::new(),
    })
    .len();
    let offset = 9 + prefix - 4;
    let len = u32::from_le_bytes(bad_map[offset..offset + 4].try_into().unwrap());
    bad_map[offset..offset + 4].copy_from_slice(&(len - 1).to_le_bytes());
    fn string(value: &str) -> Vec<u8> {
        let mut bytes = (value.len() as u32).to_le_bytes().to_vec();
        bytes.extend(value.as_bytes());
        bytes
    }
    let mut duplicate = vec![];
    for (key, value) in [
        ("source", "east"),
        ("event_type", "can.tx"),
        ("clock", "qemu:east:reset0"),
        ("source", "east"),
    ] {
        duplicate.extend(string(key));
        duplicate.extend(string(value));
    }
    let mut body = vec![1, 0, 0, 0];
    body.extend(string("fdcan1"));
    body.extend(string(ENCODING));
    body.extend((duplicate.len() as u32).to_le_bytes());
    body.extend(duplicate);
    for records in [
        vec![padded],
        vec![header(), bad_map],
        vec![header(), record(4, &body)],
        vec![header(), header()],
        vec![channel()],
        vec![header(), message(0, 23)],
        vec![header(), channel(), message(1, 23)],
        vec![header(), channel(), message(0, 24)],
        vec![header(), record(5, &[0; 21])],
    ] {
        assert!(read(&container(records)).is_err());
    }
}

struct BoundedInput {
    raw: Cursor<Vec<u8>>,
    requests: Vec<usize>,
    chunk: usize,
}
impl Read for BoundedInput {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        assert!(bytes.len() <= MAX_RECORD_BYTES + 9);
        self.requests.push(bytes.len());
        let count = bytes.len().min(self.chunk);
        self.raw.read(&mut bytes[..count])
    }
}
#[test]
fn framing_rejects_before_body_io_and_accepts_partial_blocking_reads() {
    for (opcode, len) in [(0x7f, 8), (1, 1u64 << 40)] {
        let mut bytes = mcap::MAGIC.to_vec();
        bytes.push(opcode);
        bytes.extend(len.to_le_bytes());
        bytes.extend([0; 8]);
        let mut input = BoundedInput {
            raw: Cursor::new(bytes),
            requests: vec![],
            chunk: usize::MAX,
        };
        assert!(iter_events(&mut input)
            .collect::<TraceResult<Vec<_>>>()
            .is_err());
        assert_eq!(input.raw.position(), 17);
    }
    let expected = events();
    let mut input = BoundedInput {
        raw: Cursor::new(written(&expected)),
        requests: vec![],
        chunk: 1,
    };
    let mut reader = iter_events(&mut input);
    assert_eq!(
        reader.by_ref().collect::<TraceResult<Vec<_>>>().unwrap(),
        expected
    );
    assert!(reader.is_complete());
}

fn python() -> PathBuf {
    std::env::var_os("SIM_TRACE_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let local = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/python");
            if local.is_file() {
                local
            } else {
                PathBuf::from("python3")
            }
        })
}
#[test]
fn python_native_reader_and_writer_interoperate_with_rust() {
    let temp = tempfile::tempdir().unwrap();
    let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/trace_compat.py");
    let from_python = temp.path().join("python.mcap");
    let from_rust = temp.path().join("rust.mcap");
    assert!(Command::new(python())
        .arg(&fixture)
        .arg("write")
        .arg(&from_python)
        .status()
        .unwrap()
        .success());
    assert_eq!(
        read(&std::fs::read(from_python).unwrap()).unwrap(),
        events()
    );
    std::fs::write(&from_rust, written(&events())).unwrap();
    assert!(Command::new(python())
        .arg(fixture)
        .arg("read")
        .arg(from_rust)
        .status()
        .unwrap()
        .success());
}

#[cfg(unix)]
#[test]
fn bounded_nested_length() {
    if std::env::var_os("SIM_TRACE_NESTED_CHILD").is_none() {
        let result=Command::new(python()).arg("-c").arg("import os,resource,sys; resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024)); os.execv(sys.argv[1],sys.argv[1:])").arg(std::env::current_exe().unwrap()).args(["--exact","bounded_nested_length","--nocapture"]).env("SIM_TRACE_NESTED_CHILD","1").output().unwrap();
        assert!(
            result.status.success(),
            "bounded child failed: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        return;
    }
    let mut bytes = mcap::MAGIC.to_vec();
    bytes.extend(record(1, &[255, 255, 255, 255, 0, 0, 0, 0]));
    let mut input = BoundedInput {
        raw: Cursor::new(bytes),
        requests: vec![],
        chunk: usize::MAX,
    };
    assert!(iter_events(&mut input)
        .collect::<TraceResult<Vec<_>>>()
        .is_err());
    assert_eq!(input.raw.position(), 25);
}
