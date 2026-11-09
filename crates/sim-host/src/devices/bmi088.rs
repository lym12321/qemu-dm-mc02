//! The sole SI-to-BMI088 unit boundary. Native data remains sensor-local;
//! electrical sample IDs, sensor quantization and CPU progress belong elsewhere.
use crate::signals::ImuSample;
use sim_core::{Error, Result};
use std::sync::Arc;

pub fn imu_to_bmi088_units(sample: &ImuSample) -> Result<([f32; 3], [f32; 3])> {
    sample.validate()?;
    let gyro = sample
        .gyro_rad_s
        .map(|v| (v * 180.0 / std::f64::consts::PI) as f32);
    let accel = sample.accel_m_s2.map(|v| (v / 9.80665) as f32);
    if gyro.iter().chain(&accel).any(|v| !v.is_finite()) {
        return Err(Arc::new(Error::Admission(
            "BMI088 wire values must fit finite binary32".into(),
        )));
    }
    Ok((gyro, accel))
}
