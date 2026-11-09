//! Completed sensor-local SI observations shared by physics and signal nets.
//! No frame rotation, gravity removal, quantization or electrical sample ID.

use sim_core::{Error, Result};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ImuSample {
    pub gyro_rad_s: [f64; 3],
    pub accel_m_s2: [f64; 3],
}
impl ImuSample {
    pub fn validate(&self) -> Result<()> {
        if self
            .gyro_rad_s
            .iter()
            .chain(&self.accel_m_s2)
            .any(|value| !value.is_finite())
        {
            return Err(Arc::new(Error::Admission(
                "IMU vectors must be finite SI values".into(),
            )));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ImuSignalSample {
    pub sample: ImuSample,
    pub source_time_ns: Option<u64>,
    pub mapped_time_ns: u64,
}
impl ImuSignalSample {
    pub fn validate(&self) -> Result<()> {
        self.sample.validate()
    }
}
