//! Shared DM MIT/direct-effort policy; native physics and ROS retain their state.
use sim_core::{Error, Failure, Result};
use std::sync::Arc;
fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MitCommand {
    pub position: f64,
    pub speed: f64,
    pub kp: f64,
    pub kd: f64,
    pub torque: f64,
}
impl MitCommand {
    pub fn validate(&self) -> Result<()> {
        if ![self.position, self.speed, self.kp, self.kd, self.torque]
            .iter()
            .all(|v| v.is_finite())
            || !(0.0..=500.0).contains(&self.kp)
            || !(0.0..=5.0).contains(&self.kd)
        {
            return Err(admission(
                "MIT command requires finite values, kp0..500 and kd0..5",
            ));
        }
        Ok(())
    }
}
fn limit(value: f64) -> Result<()> {
    if !value.is_finite() || value <= 0.0 {
        Err(admission("motor torque limit must be finite and positive"))
    } else {
        Ok(())
    }
}
pub fn dm_mit_effort(
    command: MitCommand,
    position: f64,
    velocity: f64,
    torque_limit: f64,
) -> Result<f64> {
    command.validate()?;
    limit(torque_limit)?;
    if !position.is_finite() || !velocity.is_finite() {
        return Err(admission("motor observations must be finite"));
    }
    let effort = command.torque
        + command.kp * (command.position - position)
        + command.kd * (command.speed - velocity);
    if !effort.is_finite() {
        return Err(admission("MIT effort overflow"));
    }
    Ok(effort.clamp(-torque_limit, torque_limit))
}
pub fn legacy_motor_effort(value: f64, enabled: bool, torque_limit: f64) -> Result<f64> {
    limit(torque_limit)?;
    if !value.is_finite() {
        return Err(admission("legacy motor effort must be finite"));
    }
    Ok(if enabled {
        value.clamp(-torque_limit, torque_limit)
    } else {
        0.0
    })
}
#[derive(Clone, Debug)]
pub struct MotorPolicy {
    torque_limit: f64,
    legacy: f64,
    enabled: bool,
    command: Option<MitCommand>,
}
impl MotorPolicy {
    pub fn new(torque_limit: f64) -> Result<Self> {
        limit(torque_limit)?;
        Ok(Self {
            torque_limit,
            legacy: 0.0,
            enabled: false,
            command: None,
        })
    }
    pub fn reset(&mut self) {
        self.legacy = 0.0;
        self.enabled = false;
        self.command = None;
    }
    pub fn enabled(&self) -> bool {
        self.enabled
    }
    pub fn command(&self) -> Option<MitCommand> {
        self.command
    }
    pub fn legacy_value(&self) -> f64 {
        self.legacy
    }
    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }
    pub fn set_legacy(&mut self, value: f64) -> Result<()> {
        legacy_motor_effort(value, false, self.torque_limit)?;
        self.legacy = value;
        self.command = None;
        Ok(())
    }
    pub fn set_mit(&mut self, command: MitCommand) -> Result<()> {
        command.validate()?;
        self.command = Some(command);
        Ok(())
    }
    pub fn effort(&self, position: f64, velocity: f64) -> Result<f64> {
        match self.command {
            Some(command) if self.enabled => {
                dm_mit_effort(command, position, velocity, self.torque_limit)
            }
            Some(_) => Ok(0.0),
            None => legacy_motor_effort(self.legacy, self.enabled, self.torque_limit),
        }
    }
}
