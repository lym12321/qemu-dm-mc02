//! DJI publisher C620 V1.01 manual pp14–17/31–33, PDF SHA256
//! f422c7a604bc5003fde4ddd3bc1329501a39bb03120020b5ac95587f0a70f71b.
//! Signed feedback is cross-checked with RoboRTS8cffd936b503eef819fa05265cf0162710c9cb3b.
//! Feedback current scale20/16384 A follows the actual trobot consumer.

use crate::{can::CanFrame, Error, Result};

pub fn validate_motor_id(motor_id: u8) -> Result<()> {
    if (1..=8).contains(&motor_id) {
        Ok(())
    } else {
        Err(Error::Admission("M3508 motor ID must be 1..8").into())
    }
}

/// A relevant command validates all four slots before accepting this ID.
pub fn decode_current_command(frame: &CanFrame, motor_id: u8) -> Result<Option<i16>> {
    validate_motor_id(motor_id)?;
    if frame.can_id() != if motor_id <= 4 { 0x200 } else { 0x1ff } {
        return Ok(None);
    }
    if frame.flags() != 0 || frame.dlc() != 8 || frame.data().len() != 8 {
        return Err(
            Error::Admission("C620 commands require standard DATA DLC8 with eight bytes").into(),
        );
    }
    let mut slots = [0i16; 4];
    for (slot, pair) in slots.iter_mut().zip(frame.data().chunks_exact(2)) {
        *slot = i16::from_be_bytes([pair[0], pair[1]]);
        if !(-16384..=16384).contains(slot) {
            return Err(Error::Admission("C620 current command must be -16384..16384").into());
        }
    }
    Ok(Some(slots[usize::from((motor_id - 1) % 4)]))
}

pub fn encode_feedback(
    motor_id: u8,
    encoder: u16,
    speed_rpm: i16,
    current_raw: i16,
    temperature_c: u8,
    timestamp_ns: u64,
) -> Result<CanFrame> {
    validate_motor_id(motor_id)?;
    if encoder > 8191 {
        return Err(Error::Admission("C620 encoder must be 0..8191").into());
    }
    let mut bytes = Vec::with_capacity(8);
    bytes.extend_from_slice(&encoder.to_be_bytes());
    bytes.extend_from_slice(&speed_rpm.to_be_bytes());
    bytes.extend_from_slice(&current_raw.to_be_bytes());
    bytes.extend_from_slice(&[temperature_c, 0]);
    CanFrame::new(0x200 + u32::from(motor_id), 0, Some(8), bytes, timestamp_ns)
}
