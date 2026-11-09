//! robot-sim/trace/1 uses the official MCAP record codec and CRC reader.
//! IO may stay borrowed. Events yielded before EOF are provisional: callers
//! must exhaust the iterator before accepting the container or causing replay IO.

use binrw::{BinRead, BinReaderExt, BinWrite, BinWriterExt};
use mcap::{
    records::{self, op},
    sans_io::{LinearReadEvent, LinearReader, LinearReaderOptions},
    write::NoSeek,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
    io::{self, Cursor, Read, Write},
    sync::Arc,
};

pub const PROFILE: &str = "robot-sim/trace/1";
pub const ENCODING: &str = "application/octet-stream";
pub const MAX_LABEL_BYTES: usize = 255;
pub const MAX_PAYLOAD_BYTES: usize = 65536;
pub const MAX_CHANNELS: usize = 256;
pub const MAX_RECORD_BYTES: usize = MAX_PAYLOAD_BYTES + 22;

#[derive(Debug)]
pub enum TraceError {
    Admission(&'static str),
    State(&'static str),
    Malformed(&'static str),
    Io(io::Error),
    Mcap(mcap::McapError),
    Record(binrw::Error),
}
impl fmt::Display for TraceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Admission(message) | Self::State(message) | Self::Malformed(message) => {
                f.write_str(message)
            }
            Self::Io(error) => error.fmt(f),
            Self::Mcap(error) => error.fmt(f),
            Self::Record(error) => error.fmt(f),
        }
    }
}
impl std::error::Error for TraceError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Mcap(error) => Some(error),
            Self::Record(error) => Some(error),
            _ => None,
        }
    }
}
pub type TraceFailure = Arc<TraceError>;
pub type TraceResult<T> = std::result::Result<T, TraceFailure>;
fn malformed(message: &'static str) -> TraceFailure {
    Arc::new(TraceError::Malformed(message))
}
fn label(value: &str) -> TraceResult<()> {
    if value.is_empty() || value.len() > MAX_LABEL_BYTES || value.contains('\0') {
        Err(Arc::new(TraceError::Admission(
            "trace labels require 1..255 NUL-free UTF-8 bytes",
        )))
    } else {
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TraceEvent {
    timestamp_ns: u64,
    source: String,
    endpoint: String,
    event_type: String,
    payload: Vec<u8>,
    clock: String,
}
impl TraceEvent {
    pub fn new(
        timestamp_ns: u64,
        source: impl Into<String>,
        endpoint: impl Into<String>,
        event_type: impl Into<String>,
        payload: impl Into<Vec<u8>>,
        clock: impl Into<String>,
    ) -> TraceResult<Self> {
        let event = Self {
            timestamp_ns,
            source: source.into(),
            endpoint: endpoint.into(),
            event_type: event_type.into(),
            payload: payload.into(),
            clock: clock.into(),
        };
        for value in [
            &event.source,
            &event.endpoint,
            &event.event_type,
            &event.clock,
        ] {
            label(value)?;
        }
        if event.payload.len() > MAX_PAYLOAD_BYTES {
            return Err(Arc::new(TraceError::Admission(
                "trace payload exceeds 65536 bytes",
            )));
        }
        Ok(event)
    }
    pub fn timestamp_ns(&self) -> u64 {
        self.timestamp_ns
    }
    pub fn source(&self) -> &str {
        &self.source
    }
    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }
    pub fn event_type(&self) -> &str {
        &self.event_type
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
    pub fn clock(&self) -> &str {
        &self.clock
    }
    fn identity(&self) -> Identity {
        (
            self.source.clone(),
            self.endpoint.clone(),
            self.event_type.clone(),
            self.clock.clone(),
        )
    }
}
type Identity = (String, String, String, String);

/// Native write_all may retry partial writes. This profile treats the first
/// short external write as terminal, preserving the partial effect exactly.
struct Output<W: Write> {
    stream: W,
}
impl<W: Write> Write for Output<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let count = self.stream.write(bytes)?;
        if count != bytes.len() {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "short trace write; output is incomplete",
            ));
        }
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.stream.flush()
    }
}

/// Single append owner. Admission errors leave it usable; attempted write or
/// flush failures freeze it, without retry, rollback or closing borrowed IO.
pub struct TraceWriter<W: Write> {
    writer: Option<mcap::Writer<NoSeek<Output<W>>>>,
    channels: BTreeMap<Identity, u16>,
    sequence: u64,
    failure: Option<TraceFailure>,
    finished: bool,
}
impl<W: Write> TraceWriter<W> {
    pub fn new(output: W) -> TraceResult<Self> {
        let writer = mcap::WriteOptions::new()
            .profile(PROFILE)
            .library("robot-sim/rust-mcap-0.26.0")
            .compression(None)
            .use_chunks(false)
            .disable_seeking(true)
            .emit_summary_records(false)
            .emit_summary_offsets(false)
            .emit_message_indexes(false)
            .calculate_chunk_crcs(false)
            .calculate_data_section_crc(true)
            .calculate_summary_section_crc(false)
            .calculate_attachment_crcs(false)
            .create(NoSeek::new(Output { stream: output }))
            .map_err(|error| Arc::new(TraceError::Mcap(error)))?;
        Ok(Self {
            writer: Some(writer),
            channels: BTreeMap::new(),
            sequence: 0,
            failure: None,
            finished: false,
        })
    }
    fn active(&self) -> TraceResult<()> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.finished {
            return Err(Arc::new(TraceError::State("trace writer is finished")));
        }
        Ok(())
    }
    fn freeze(&mut self, error: TraceFailure) -> TraceFailure {
        if let Some(writer) = self.writer.take() {
            let _ = writer.into_inner();
        }
        self.failure.get_or_insert(error).clone()
    }
    pub fn append(&mut self, event: &TraceEvent) -> TraceResult<()> {
        self.active()?;
        let identity = event.identity();
        let known = self.channels.get(&identity).copied();
        if known.is_none() && self.channels.len() >= MAX_CHANNELS {
            return Err(Arc::new(TraceError::Admission(
                "trace channel capacity exhausted",
            )));
        }
        let sequence = u32::try_from(self.sequence)
            .map_err(|_| Arc::new(TraceError::Admission("trace sequence exhausted")))?;
        let result = (|| {
            let writer = self.writer.as_mut().expect("active trace writer");
            let channel = if let Some(channel) = known {
                channel
            } else {
                let metadata = BTreeMap::from([
                    ("source".to_owned(), event.source.clone()),
                    ("event_type".to_owned(), event.event_type.clone()),
                    ("clock".to_owned(), event.clock.clone()),
                ]);
                writer.add_channel(0, &event.endpoint, ENCODING, &metadata)?
            };
            writer.write_to_known_channel(
                &records::MessageHeader {
                    channel_id: channel,
                    sequence,
                    log_time: event.timestamp_ns,
                    publish_time: event.timestamp_ns,
                },
                &event.payload,
            )?;
            Ok::<_, mcap::McapError>(channel)
        })();
        match result {
            Ok(channel) => {
                self.channels.insert(identity, channel);
                self.sequence += 1;
                Ok(())
            }
            Err(error) => Err(self.freeze(Arc::new(TraceError::Mcap(error)))),
        }
    }
    pub fn finish(&mut self) -> TraceResult<()> {
        if self.finished {
            return Ok(());
        }
        self.active()?;
        if let Err(error) = self.writer.as_mut().expect("active trace writer").finish() {
            return Err(self.freeze(Arc::new(TraceError::Mcap(error))));
        }
        let mut output = self
            .writer
            .take()
            .expect("active trace writer")
            .into_inner();
        if let Err(error) = output.flush() {
            return Err(self.freeze(Arc::new(TraceError::Io(error))));
        }
        self.finished = true;
        Ok(())
    }
}
impl<W: Write> Drop for TraceWriter<W> {
    fn drop(&mut self) {
        // The official into_inner explicitly suppresses Writer's automatic
        // finish. Dropping an unfinished trace must not publish completion.
        if let Some(writer) = self.writer.take() {
            let _ = writer.into_inner();
        }
    }
}

/// Framing admission only. The native MCAP reader owns magic/record decoding,
/// CRC and EOF. Reject unknown envelopes and excessive lengths before body IO.
enum Envelope {
    Magic(usize),
    Header { bytes: [u8; 9], filled: usize },
    Body { remaining: usize, footer: bool },
    Trailer(usize),
    Eof,
}
fn header() -> Envelope {
    Envelope::Header {
        bytes: [0; 9],
        filled: 0,
    }
}
struct Input<'a, R: Read + ?Sized> {
    stream: &'a mut R,
    envelope: Envelope,
}
impl<R: Read + ?Sized> Read for Input<'_, R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let maximum = match &self.envelope {
            Envelope::Magic(left) | Envelope::Trailer(left) => *left,
            Envelope::Header { filled, .. } => 9 - filled,
            Envelope::Body { remaining, .. } => *remaining,
            Envelope::Eof => 1,
        };
        let size = output.len().min(maximum);
        let count = self.stream.read(&mut output[..size])?;
        match &mut self.envelope {
            Envelope::Magic(left) => {
                *left -= count;
                if *left == 0 {
                    self.envelope = header();
                }
            }
            Envelope::Header { bytes, filled } => {
                bytes[*filled..*filled + count].copy_from_slice(&output[..count]);
                *filled += count;
                if *filled == 9 {
                    if !matches!(
                        bytes[0],
                        op::HEADER | op::CHANNEL | op::MESSAGE | op::DATA_END | op::FOOTER
                    ) {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "unsupported trace MCAP record",
                        ));
                    }
                    let length = u64::from_le_bytes(
                        bytes[1..].try_into().expect("complete record envelope"),
                    );
                    if length == 0 || length > MAX_RECORD_BYTES as u64 {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "trace MCAP record size exceeds profile bounds",
                        ));
                    }
                    self.envelope = Envelope::Body {
                        remaining: length as usize,
                        footer: bytes[0] == op::FOOTER,
                    };
                }
            }
            Envelope::Body { remaining, footer } => {
                *remaining -= count;
                if *remaining == 0 {
                    self.envelope = if *footer {
                        Envelope::Trailer(8)
                    } else {
                        header()
                    };
                }
            }
            Envelope::Trailer(left) => {
                *left -= count;
                if *left == 0 {
                    self.envelope = Envelope::Eof;
                }
            }
            Envelope::Eof => {}
        }
        Ok(count)
    }
}

fn encoded<T: for<'a> BinWrite<Args<'a> = ()>>(value: &T) -> TraceResult<Vec<u8>> {
    let mut output = Cursor::new(Vec::new());
    output
        .write_le(value)
        .map_err(|error| Arc::new(TraceError::Record(error)))?;
    Ok(output.into_inner())
}
fn parsed<T: for<'a> BinRead<Args<'a> = ()>>(data: &[u8]) -> TraceResult<T> {
    let mut input = Cursor::new(data);
    let value: T = input
        .read_le()
        .map_err(|error| Arc::new(TraceError::Record(error)))?;
    if input.position() != data.len() as u64 {
        return Err(malformed("noncanonical trace MCAP record"));
    }
    Ok(value)
}
fn canonical<T: for<'a> BinRead<Args<'a> = ()> + for<'a> BinWrite<Args<'a> = ()>>(
    data: &[u8],
) -> TraceResult<T> {
    let value = parsed(data)?;
    if encoded(&value)? != data {
        return Err(malformed("noncanonical trace MCAP record"));
    }
    Ok(value)
}
fn channel(data: &[u8]) -> TraceResult<records::Channel> {
    let value: records::Channel = parsed(data)?;
    let canonical = encoded(&value)?;
    let mut empty = value.clone();
    empty.metadata.clear();
    let prefix = encoded(&empty)?.len();
    // Metadata is a map with unspecified entry order. The official decoder
    // rejects duplicate keys. Native encoding of an empty map locates the end
    // of the map-length prefix without duplicating its binary field parser.
    if canonical.len() != data.len() || canonical[..prefix] != data[..prefix] {
        return Err(malformed("noncanonical trace MCAP channel"));
    }
    Ok(value)
}

pub struct TraceReader<'a, R: Read + ?Sized> {
    input: Input<'a, R>,
    reader: LinearReader,
    channels: BTreeMap<u16, Identity>,
    identities: BTreeSet<Identity>,
    sequence: u64,
    header: bool,
    data_end: bool,
    footer: bool,
    complete: bool,
    failed: bool,
}
impl<'a, R: Read + ?Sized> TraceReader<'a, R> {
    pub fn new(input: &'a mut R) -> Self {
        Self {
            input: Input {
                stream: input,
                envelope: Envelope::Magic(8),
            },
            reader: LinearReader::new_with_options(
                LinearReaderOptions::default()
                    .with_emit_chunks(true)
                    .with_record_length_limit(MAX_RECORD_BYTES)
                    .with_validate_data_section_crc(true)
                    .with_validate_summary_section_crc(true)
                    .with_check_finishes_after_end_magic(true),
            ),
            channels: BTreeMap::new(),
            identities: BTreeSet::new(),
            sequence: 0,
            header: false,
            data_end: false,
            footer: false,
            complete: false,
            failed: false,
        }
    }
    pub fn is_complete(&self) -> bool {
        self.complete
    }
    fn record(&mut self, opcode: u8, data: &[u8]) -> TraceResult<Option<TraceEvent>> {
        if !self.header {
            if opcode != op::HEADER {
                return Err(malformed("unsupported trace MCAP profile/header"));
            }
            let value: records::Header = canonical(data)?;
            if value.profile != PROFILE || value.library.len() > MAX_LABEL_BYTES {
                return Err(malformed("unsupported trace MCAP profile/header"));
            }
            self.header = true;
            return Ok(None);
        }
        match opcode {
            op::CHANNEL if !self.data_end => {
                let value = channel(data)?;
                if value.id == 0
                    || self.channels.contains_key(&value.id)
                    || value.schema_id != 0
                    || value.message_encoding != ENCODING
                    || value.metadata.len() != 3
                    || !["source", "event_type", "clock"]
                        .iter()
                        .all(|key| value.metadata.contains_key(*key))
                {
                    return Err(malformed("invalid trace MCAP channel"));
                }
                let identity = (
                    value.metadata["source"].clone(),
                    value.topic,
                    value.metadata["event_type"].clone(),
                    value.metadata["clock"].clone(),
                );
                for value in [&identity.0, &identity.1, &identity.2, &identity.3] {
                    label(value)?;
                }
                if self.channels.len() >= MAX_CHANNELS || !self.identities.insert(identity.clone())
                {
                    return Err(malformed(
                        "duplicate trace channel identity or capacity exhausted",
                    ));
                }
                self.channels.insert(value.id, identity);
                Ok(None)
            }
            op::MESSAGE if !self.data_end => {
                let mut input = Cursor::new(data);
                let message: records::MessageHeader = input
                    .read_le()
                    .map_err(|error| Arc::new(TraceError::Record(error)))?;
                let identity = self
                    .channels
                    .get(&message.channel_id)
                    .ok_or_else(|| malformed("trace message refers to an unknown channel"))?;
                if u64::from(message.sequence) != self.sequence
                    || message.publish_time != message.log_time
                {
                    return Err(malformed("invalid trace MCAP message sequence/time"));
                }
                let event = TraceEvent::new(
                    message.log_time,
                    &identity.0,
                    &identity.1,
                    &identity.2,
                    data[input.position() as usize..].to_vec(),
                    &identity.3,
                )?;
                self.sequence += 1;
                Ok(Some(event))
            }
            op::DATA_END if !self.data_end => {
                let _: records::DataEnd = canonical(data)?;
                self.data_end = true;
                Ok(None)
            }
            op::FOOTER if self.data_end => {
                let footer: records::Footer = canonical(data)?;
                if footer.summary_start != 0
                    || footer.summary_offset_start != 0
                    || footer.summary_crc != 0
                {
                    return Err(malformed("trace MCAP summaries are unsupported"));
                }
                self.footer = true;
                Ok(None)
            }
            _ => Err(malformed("invalid trace MCAP record order")),
        }
    }
    fn read_next(&mut self) -> TraceResult<Option<TraceEvent>> {
        loop {
            match self.reader.next_event() {
                Some(Ok(LinearReadEvent::ReadRequest(need))) => {
                    if need > MAX_RECORD_BYTES + 9 {
                        return Err(malformed("trace MCAP read request exceeds profile bounds"));
                    }
                    let count = self
                        .input
                        .read(self.reader.insert(need))
                        .map_err(|error| Arc::new(TraceError::Io(error)))?;
                    self.reader.notify_read(count);
                }
                Some(Ok(LinearReadEvent::Record { opcode, data })) => {
                    // Split the mutable reader borrow from profile admission.
                    // Only one bounded record is copied; no event history lives here.
                    let data = data.to_vec();
                    if let Some(event) = self.record(opcode, &data)? {
                        return Ok(Some(event));
                    }
                }
                Some(Err(error)) => return Err(Arc::new(TraceError::Mcap(error))),
                None => {
                    if !(self.header && self.data_end && self.footer) {
                        return Err(malformed("incomplete trace MCAP container"));
                    }
                    self.complete = true;
                    return Ok(None);
                }
            }
        }
    }
}
impl<R: Read + ?Sized> Iterator for TraceReader<'_, R> {
    type Item = TraceResult<TraceEvent>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.complete || self.failed {
            return None;
        }
        match self.read_next() {
            Ok(Some(event)) => Some(Ok(event)),
            Ok(None) => None,
            Err(error) => {
                self.failed = true;
                Some(Err(error))
            }
        }
    }
}
pub fn iter_events<R: Read + ?Sized>(input: &mut R) -> TraceReader<'_, R> {
    TraceReader::new(input)
}
