//! C620 wire policy and completed M3508 rotor observations. Physics belongs to
//! the consumer, including any gearbox and shared-world stepping.
mod device;
pub mod joints;
pub mod protocol;
pub use device::{DeviceOptions, DeviceState, DrivePlan, M3508Device, M3508};
