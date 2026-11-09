//! Pure complete-frame envelope, RoboMaster 2026 V1.1.0 (20251217), p4/pp77–82.
//! Payload is opaque; no stream parsing, sequence ownership or rule policy.
//! CRC algorithms reuse crc 3.4.0 catalog entries, without local recurrence.

use crc::{Crc, CRC_16_MCRF4XX, CRC_8_MAXIM_DOW};
use sim_core::{Error, Result};
use std::sync::Arc;

pub const MAX_FRAME_BYTES: usize = 128;
pub const MAX_PAYLOAD_BYTES: usize = MAX_FRAME_BYTES - 9;
const HEADER_CRC: Crc<u8> = Crc::<u8>::new(&CRC_8_MAXIM_DOW);
const FRAME_CRC: Crc<u16> = Crc::<u16>::new(&CRC_16_MCRF4XX);
fn header_crc(bytes: &[u8]) -> u8 {
    let mut digest = HEADER_CRC.digest_with_initial(0xff);
    digest.update(bytes);
    digest.finalize()
}
fn admission(message: &str) -> sim_core::Failure {
    Arc::new(Error::Admission(message.into()))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Frame {
    pub command_id: u16,
    pub payload: Vec<u8>,
    pub sequence: u8,
}

pub fn encode_frame(command_id: u16, payload: &[u8], sequence: u8) -> Result<Vec<u8>> {
    if payload.len() > MAX_PAYLOAD_BYTES {
        return Err(admission("referee payload exceeds 119 bytes"));
    }
    let mut raw = Vec::with_capacity(payload.len() + 9);
    raw.push(0xa5);
    raw.extend_from_slice(&(payload.len() as u16).to_le_bytes());
    raw.push(sequence);
    raw.push(header_crc(&raw));
    raw.extend_from_slice(&command_id.to_le_bytes());
    raw.extend_from_slice(payload);
    raw.extend_from_slice(&FRAME_CRC.checksum(&raw).to_le_bytes());
    Ok(raw)
}
pub fn decode_frame(raw: &[u8]) -> Result<Frame> {
    if !(9..=MAX_FRAME_BYTES).contains(&raw.len()) {
        return Err(admission(
            "referee frame length is outside the supported profile",
        ));
    }
    if raw[0] != 0xa5 {
        return Err(admission("referee frame SOF must be 0xa5"));
    }
    let length = u16::from_le_bytes([raw[1], raw[2]]) as usize;
    if length + 9 != raw.len() {
        return Err(admission(
            "referee payload length does not match the complete frame",
        ));
    }
    if header_crc(&raw[..4]) != raw[4] {
        return Err(admission("referee header CRC8 mismatch"));
    }
    if FRAME_CRC.checksum(&raw[..raw.len() - 2])
        != u16::from_le_bytes([raw[raw.len() - 2], raw[raw.len() - 1]])
    {
        return Err(admission("referee frame CRC16 mismatch"));
    }
    Ok(Frame {
        command_id: u16::from_le_bytes([raw[5], raw[6]]),
        payload: raw[7..raw.len() - 2].to_vec(),
        sequence: raw[3],
    })
}
