//! Existing DM-MC02 FDCAN chardev record, explicitly little endian.
//! Layout: id u32, flags u32, DLC u8, reserved[3], timestamp u64, data[64].
//! This metadata is independent of physical CAN arbitration and CPU execution.

use super::stream::StreamTransport;
use crate::{
    can::{dlc_to_length, CanFrame, FD},
    Error, Result,
};
use std::{
    io::{Read, Write},
    sync::Arc,
};

pub const WIRE_SIZE: usize = 84;

pub fn encode(frame: &CanFrame) -> [u8; WIRE_SIZE] {
    let mut bytes = [0; WIRE_SIZE];
    bytes[..4].copy_from_slice(&frame.can_id().to_le_bytes());
    bytes[4..8].copy_from_slice(&frame.flags().to_le_bytes());
    bytes[8] = frame.dlc();
    bytes[12..20].copy_from_slice(&frame.timestamp_ns().to_le_bytes());
    bytes[20..20 + frame.data().len()].copy_from_slice(frame.data());
    bytes
}

pub fn decode(bytes: &[u8]) -> Result<CanFrame> {
    if bytes.len() != WIRE_SIZE || bytes[9..12] != [0; 3] {
        return Err(Arc::new(Error::Admission(
            "invalid fixed84 length or reserved bytes",
        )));
    }
    let can_id = u32::from_le_bytes(bytes[..4].try_into().expect("four-byte ID"));
    let flags = u32::from_le_bytes(bytes[4..8].try_into().expect("four-byte flags"));
    let length = dlc_to_length(bytes[8], flags & FD != 0)?;
    let timestamp = u64::from_le_bytes(bytes[12..20].try_into().expect("eight-byte timestamp"));
    CanFrame::new(
        can_id,
        flags,
        Some(bytes[8]),
        bytes[20..20 + length].to_vec(),
        timestamp,
    )
}

pub trait CanFrameSink {
    fn send_frame(&mut self, frame: &CanFrame) -> Result<()>;
}

/// The stream owns pending records while borrowing the caller's nonblocking IO.
pub struct CanStream<T: Read + Write> {
    transport: StreamTransport<T>,
}

impl<T: Read + Write> CanStream<T> {
    pub fn new(io: T, max_pending: usize) -> Result<Self> {
        Ok(Self {
            transport: StreamTransport::new(io, max_pending)?,
        })
    }
    pub fn transport(&self) -> &StreamTransport<T> {
        &self.transport
    }
    pub fn transport_mut(&mut self) -> &mut StreamTransport<T> {
        &mut self.transport
    }

    pub fn receive(&mut self) -> Result<Vec<CanFrame>> {
        self.transport.read_once()?;
        let mut frames = Vec::new();
        while let Some(bytes) = self.transport.next_record(WIRE_SIZE)? {
            frames.push(decode(&bytes)?);
        }
        Ok(frames)
    }
}

impl<T: Read + Write> CanFrameSink for CanStream<T> {
    fn send_frame(&mut self, frame: &CanFrame) -> Result<()> {
        self.transport.send(encode(frame).to_vec())
    }
}
