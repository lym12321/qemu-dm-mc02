//! Legacy worker motor policy over the sole native ROS transport owner.
use super::{Ros2Options, Ros2Snapshot, Ros2Transport};
use crate::{
    worker::motor_policy::{MitCommand, MotorPolicy},
    Error, Failure, Result,
};
use std::{sync::Arc, time::Duration};

pub struct Ros2Engine {
    transport: Ros2Transport,
    motors: Vec<MotorPolicy>,
    commanded_efforts: Vec<f64>,
    wait_for_imu: bool,
    reset_timeout: Duration,
}

fn policy_error(error: sim_core::Failure) -> Failure {
    Arc::new(Error::Callback(error.to_string()))
}

impl Ros2Engine {
    pub fn new(
        options: &Ros2Options,
        torque_limit: f64,
        wait_for_imu: bool,
        reset_timeout: Duration,
    ) -> Result<Self> {
        options.validate()?;
        if reset_timeout.is_zero() {
            return Err(Arc::new(Error::Admission(
                "ROS2 reset timeout must be positive",
            )));
        }
        super::duration_ns(reset_timeout)?;
        let motor = MotorPolicy::new(torque_limit).map_err(policy_error)?;
        let transport = Ros2Transport::new(options)?;
        Ok(Self {
            transport,
            motors: vec![motor; options.motor_count],
            commanded_efforts: vec![0.0; options.motor_count],
            wait_for_imu,
            reset_timeout,
        })
    }

    pub fn reset(&mut self) -> Result<()> {
        for motor in &mut self.motors {
            motor.reset();
        }
        self.commanded_efforts.fill(0.0);
        self.transport.reset(self.reset_timeout)
    }

    fn publish(&mut self, index: usize) -> Result<()> {
        let state = self.transport.sample()?;
        self.commanded_efforts[index] = self.motors[index]
            .effort(state.position_rad[index], state.velocity_rad_s[index])
            .map_err(policy_error)?;
        self.transport.publish_efforts(&self.commanded_efforts)
    }

    pub fn set_motor(&mut self, index: usize, value: f64) -> Result<()> {
        let Some(motor) = self.motors.get_mut(index) else {
            return Ok(());
        };
        motor.set_legacy(value).map_err(policy_error)?;
        self.publish(index)
    }

    pub fn set_motor_enabled(&mut self, index: usize, enabled: bool) -> Result<()> {
        let Some(motor) = self.motors.get_mut(index) else {
            return Ok(());
        };
        motor.set_enabled(enabled);
        if motor.command().is_none() {
            self.publish(index)
        } else if !enabled {
            motor.set_legacy(0.0).map_err(policy_error)?;
            self.publish(index)
        } else {
            Ok(())
        }
    }

    pub fn set_motor_dm(&mut self, index: usize, command: MitCommand) -> Result<()> {
        let Some(motor) = self.motors.get_mut(index) else {
            return Ok(());
        };
        motor.set_mit(command).map_err(policy_error)?;
        self.publish(index)
    }

    pub fn reset_motor(&mut self, index: usize) -> Result<()> {
        let Some(motor) = self.motors.get_mut(index) else {
            return Ok(());
        };
        motor.reset();
        self.publish(index)
    }

    pub fn motor_feedback(&self, index: usize) -> Result<(f64, f64, f64)> {
        let Some(commanded) = self.commanded_efforts.get(index) else {
            return Ok((0.0, 0.0, 0.0));
        };
        let state = self.transport.sample()?;
        Ok((
            state.position_rad[index],
            state.velocity_rad_s[index],
            state.effort_nm[index].unwrap_or(*commanded),
        ))
    }

    pub fn step(&mut self, dt: Duration) -> Result<Ros2Snapshot> {
        self.transport.step(dt, self.wait_for_imu)
    }
    pub fn sample(&self) -> Result<Ros2Snapshot> {
        self.transport.sample()
    }
    pub fn close(&mut self) -> Result<()> {
        self.transport.close()
    }
}
