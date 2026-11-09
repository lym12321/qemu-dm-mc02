//! Nonblocking borrowed byte streams. Logical sessions do not clear physical
//! RX fragments or unsent TX suffixes. Socket acquisition and waits belong to
//! the caller; this helper neither closes a socket nor creates a timer.

use crate::{Error, Result};
use std::{
    collections::VecDeque,
    io::{self, Read, Write},
    sync::Arc,
};

struct Pending {
    bytes: Vec<u8>,
    offset: usize,
}

pub struct StreamTransport<T: Read + Write> {
    io: T,
    rx: VecDeque<u8>,
    tx: VecDeque<Pending>,
    max_pending: usize,
}

impl<T: Read + Write> StreamTransport<T> {
    pub fn new(io: T, max_pending: usize) -> Result<Self> {
        if max_pending == 0 {
            return Err(Arc::new(Error::Admission(
                "stream capacity must be positive",
            )));
        }
        Ok(Self {
            io,
            rx: VecDeque::new(),
            tx: VecDeque::new(),
            max_pending,
        })
    }

    pub fn io(&self) -> &T {
        &self.io
    }

    pub fn tx_pending(&self) -> bool {
        !self.tx.is_empty()
    }
    pub fn pending_records(&self) -> usize {
        self.tx.len()
    }
    pub fn buffered_bytes(&self) -> usize {
        self.rx.len()
    }

    /// A capacity refusal adds no new record. Other failures may follow partial
    /// IO; the consumer decides whether its enclosing execution must freeze.
    pub fn send(&mut self, bytes: Vec<u8>) -> Result<()> {
        if self.tx.len() >= self.max_pending {
            self.flush()?;
        }
        if self.tx.len() >= self.max_pending {
            return Err(Arc::new(Error::Capacity("stream TX queue is full")));
        }
        self.tx.push_back(Pending { bytes, offset: 0 });
        self.flush()?;
        Ok(())
    }

    pub fn flush(&mut self) -> Result<bool> {
        while let Some(pending) = self.tx.front_mut() {
            let remaining = &pending.bytes[pending.offset..];
            let written = match self.io.write(remaining) {
                Ok(0) => {
                    return Err(Arc::new(Error::Io(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "stream closed",
                    ))))
                }
                Ok(written) => written,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(false),
                Err(error) => return Err(Arc::new(Error::Io(error))),
            };
            pending.offset += written;
            if pending.offset < pending.bytes.len() {
                return Ok(false);
            }
            self.tx.pop_front();
        }
        Ok(true)
    }

    /// Exactly one read, capped at 4096 bytes, including when old records remain.
    pub fn read_once(&mut self) -> Result<()> {
        let mut bytes = [0; 4096];
        match self.io.read(&mut bytes) {
            Ok(0) => Err(Arc::new(Error::Io(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "stream closed",
            )))),
            Ok(length) => {
                self.rx.extend(&bytes[..length]);
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(()),
            Err(error) => Err(Arc::new(Error::Io(error))),
        }
    }

    /// Pop one complete opaque record so a decoder can reject it without
    /// consuming the later buffered records.
    pub fn next_record(&mut self, size: usize) -> Result<Option<Vec<u8>>> {
        if size == 0 {
            return Err(Arc::new(Error::Admission("record size must be positive")));
        }
        if self.rx.len() < size {
            return Ok(None);
        }
        Ok(Some(self.rx.drain(..size).collect()))
    }

    fn check_lengths(min_body: usize, max_body: usize) -> Result<()> {
        if min_body > max_body || max_body > u32::MAX as usize {
            return Err(Arc::new(Error::Admission("invalid length-prefix geometry")));
        }
        Ok(())
    }

    /// Admit an opaque body before flushing existing IO. The four-byte LE
    /// prefix and body occupy one existing TX slot.
    pub fn send_length_prefixed(
        &mut self,
        body: &[u8],
        min_body: usize,
        max_body: usize,
    ) -> Result<()> {
        Self::check_lengths(min_body, max_body)?;
        if !(min_body..=max_body).contains(&body.len()) {
            return Err(Arc::new(Error::Admission(
                "invalid length-prefixed body size",
            )));
        }
        let size = body
            .len()
            .checked_add(4)
            .ok_or_else(|| Arc::new(Error::Admission("length-prefixed size overflows usize")))?;
        let mut record = Vec::with_capacity(size);
        record.extend_from_slice(&(body.len() as u32).to_le_bytes());
        record.extend_from_slice(body);
        self.send(record)
    }

    /// Return one body, excluding its four-byte LE prefix. Invalid lengths and
    /// incomplete records leave all physical bytes in the same RX buffer.
    /// The caller performs read_once separately, at most once per pump.
    pub fn next_length_prefixed(
        &mut self,
        min_body: usize,
        max_body: usize,
    ) -> Result<Option<Vec<u8>>> {
        Self::check_lengths(min_body, max_body)?;
        if self.rx.len() < 4 {
            return Ok(None);
        }
        let length = u32::from_le_bytes([self.rx[0], self.rx[1], self.rx[2], self.rx[3]]) as usize;
        if !(min_body..=max_body).contains(&length) {
            return Err(Arc::new(Error::Admission(
                "invalid length-prefixed body size",
            )));
        }
        let size = length
            .checked_add(4)
            .ok_or_else(|| Arc::new(Error::Admission("length-prefixed size overflows usize")))?;
        if self.rx.len() < size {
            return Ok(None);
        }
        Ok(Some(self.rx.drain(..size).skip(4).collect()))
    }
}
