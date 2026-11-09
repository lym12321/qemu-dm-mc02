//! Native ROS 2 transport. rclcpp owns DDS, callback queues and its context;
//! the caller owns worker time, motor policy and source timestamp mapping.

mod engine;
mod ffi;
pub use engine::Ros2Engine;

use crate::{signals::ImuSample, Error, Failure, Result};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

#[derive(Clone, Debug)]
pub struct Ros2Options {
    pub motor_count: usize,
    pub motor_topic: String,
    pub imu_topic: String,
    pub joint_state_topic: String,
    pub node_name: String,
    pub reset_service: String,
}

impl Ros2Options {
    pub fn validate(&self) -> Result<()> {
        for value in [
            &self.motor_topic,
            &self.imu_topic,
            &self.joint_state_topic,
            &self.node_name,
        ] {
            if value.is_empty() || value.len() > 1024 || value.contains('\0') {
                return Err(Arc::new(Error::Admission(
                    "invalid ROS2 topic or node name",
                )));
            }
        }
        if self.reset_service.len() > 1024 || self.reset_service.contains('\0') {
            return Err(Arc::new(Error::Admission("invalid ROS2 reset service")));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Ros2Snapshot {
    pub imu: ImuSample,
    pub source_time_ns: Option<u64>,
    pub position_rad: Vec<f64>,
    pub velocity_rad_s: Vec<f64>,
    pub effort_nm: Vec<Option<f64>>,
}

pub struct Ros2Transport {
    native: cxx::UniquePtr<ffi::bridge::Transport>,
    motor_count: usize,
    failure: Option<Failure>,
    closed: bool,
}

fn native_error(error: cxx::Exception) -> Failure {
    Arc::new(Error::Callback(format!("ROS2: {error}")))
}

impl Ros2Transport {
    pub fn new(options: &Ros2Options) -> Result<Self> {
        options.validate()?;
        let native = ffi::bridge::new_transport(
            options.motor_count,
            &options.motor_topic,
            &options.imu_topic,
            &options.joint_state_topic,
            &options.node_name,
            &options.reset_service,
        )
        .map_err(native_error)?;
        Ok(Self {
            native,
            motor_count: options.motor_count,
            failure: None,
            closed: false,
        })
    }

    fn check(&self) -> Result<()> {
        if let Some(failure) = &self.failure {
            return Err(failure.clone());
        }
        if self.closed {
            return Err(Arc::new(Error::State("ROS2 transport is closed")));
        }
        Ok(())
    }

    fn finish<T>(&mut self, result: std::result::Result<T, cxx::Exception>) -> Result<T> {
        result.map_err(|error| {
            if let Some(failure) = &self.failure {
                return failure.clone();
            }
            let failure = native_error(error);
            self.failure = Some(failure.clone());
            failure
        })
    }

    /// Read completed callback state without spinning or consuming a message.
    pub fn sample(&self) -> Result<Ros2Snapshot> {
        self.check()?;
        let value = self.native.sample().map_err(native_error)?;
        Ok(Ros2Snapshot {
            imu: ImuSample {
                gyro_rad_s: value.gyro_rad_s,
                accel_m_s2: value.accel_m_s2,
            },
            source_time_ns: value.source_time_valid.then_some(value.source_time_ns),
            position_rad: value.position_rad,
            velocity_rad_s: value.velocity_rad_s,
            effort_nm: value
                .effort_nm
                .into_iter()
                .zip(value.effort_valid)
                .map(|(effort, valid)| (valid != 0).then_some(effort))
                .collect(),
        })
    }

    pub fn publish_efforts(&mut self, values: &[f64]) -> Result<()> {
        self.check()?;
        if values.len() != self.motor_count || values.iter().any(|v| !v.is_finite()) {
            return Err(Arc::new(Error::Admission(
                "ROS2 effort array must contain one finite value per motor",
            )));
        }
        let result = self.native.pin_mut().publish_efforts(values);
        self.finish(result)
    }

    /// Exactly one bounded native executor turn; zero is a nonblocking poll.
    pub fn pump(&mut self, timeout: Duration) -> Result<()> {
        self.check()?;
        let ns = duration_ns(timeout)?;
        let result = self.native.pin_mut().pump(ns);
        self.finish(result)
    }

    /// Clear held state, publish zeros, then await the optional Empty service.
    /// A timeout retains those physical effects. Reset is the failure recovery gate.
    pub fn reset(&mut self, timeout: Duration) -> Result<()> {
        if self.closed {
            return self.check();
        }
        let ns = duration_ns(timeout)?;
        if ns == 0 {
            return Err(Arc::new(Error::Admission(
                "ROS2 reset timeout must be positive",
            )));
        }
        let result = self.native.pin_mut().reset(ns);
        match self.finish(result) {
            Ok(()) => {
                self.failure = None;
                Ok(())
            }
            Err(error) => Err(error),
        }
    }

    /// Preserve the existing optional freshness wait: expiration returns held SI
    /// state; source timestamps remain native and are never synthesized.
    pub fn step(&mut self, dt: Duration, wait_for_imu: bool) -> Result<Ros2Snapshot> {
        self.check()?;
        if !wait_for_imu {
            self.pump(Duration::ZERO)?;
        } else {
            let previous = self.sample()?.source_time_ns;
            let budget = dt
                .saturating_mul(2)
                .clamp(Duration::from_millis(1), Duration::from_millis(100));
            let deadline = Instant::now() + budget;
            loop {
                let current = self.sample()?.source_time_ns;
                if current.is_some_and(|time| previous.is_none_or(|previous| time > previous)) {
                    break;
                }
                let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                    break;
                };
                self.pump(remaining.min(Duration::from_millis(1)))?;
            }
        }
        self.sample()
    }

    pub fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        self.native.pin_mut().close().map_err(native_error)
    }
}

impl Drop for Ros2Transport {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

fn duration_ns(value: Duration) -> Result<u64> {
    u64::try_from(value.as_nanos())
        .ok()
        .filter(|ns| *ns <= i64::MAX as u64)
        .ok_or_else(|| {
            Arc::new(Error::Admission(
                "ROS2 timeout exceeds native duration range",
            ))
        })
}
