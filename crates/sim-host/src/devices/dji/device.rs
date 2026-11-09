use super::protocol::{decode_current_command, encode_feedback, validate_motor_id};
use crate::{can::CanFrame, physics::rotor::Rotor};
use sim_core::{Error, Failure, Result, Session, StepContext, StepResult};
use std::{
    f64::consts::{PI, TAU},
    rc::Rc,
    sync::Arc,
};

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}
fn finite(position: f64, velocity: f64) -> Result<()> {
    if position.is_finite() && velocity.is_finite() {
        Ok(())
    } else {
        Err(admission("rotor position and velocity must be finite"))
    }
}

#[derive(Clone, Debug)]
pub struct DeviceOptions {
    pub motor_id: u8,
    pub torque_per_amp: f64,
    pub command_timeout_ns: u64,
    pub feedback_period_ns: u64,
    pub temperature_c: u8,
}
impl Default for DeviceOptions {
    fn default() -> Self {
        Self {
            motor_id: 1,
            torque_per_amp: 0.02,
            command_timeout_ns: 100_000_000,
            feedback_period_ns: 1_000_000,
            temperature_c: 25,
        }
    }
}
impl DeviceOptions {
    pub fn validate(&self) -> Result<()> {
        validate_motor_id(self.motor_id).map_err(|e| -> Failure { e })?;
        if self.torque_per_amp < 0.0
            || !(20.0 * self.torque_per_amp).is_finite()
            || self.command_timeout_ns == 0
            || self.feedback_period_ns == 0
        {
            return Err(admission(
                "M3508 needs finite nonnegative20A torque and positive timeout/feedback period",
            ));
        }
        Ok(())
    }
}

/// Not cloneable or constructible by callers. Its private identity admits only
/// the original pending plan, including after reset or another device's step.
pub struct DrivePlan {
    identity: Rc<()>,
    context: StepContext,
    active_until_ns: u64,
    effort_nm: f64,
}
impl DrivePlan {
    pub fn context(&self) -> StepContext {
        self.context
    }
    pub fn active_until_ns(&self) -> u64 {
        self.active_until_ns
    }
    pub fn effort_nm(&self) -> f64 {
        self.effort_nm
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DeviceState {
    pub current_raw: i16,
    pub rejected_commands: u64,
}

pub struct M3508Device {
    options: DeviceOptions,
    closed: bool,
    session_id: Option<u64>,
    command_raw: i16,
    command_expiry_ns: u128,
    time_ns: u64,
    step_id: u64,
    next_feedback_ns: u128,
    pending: Option<Rc<()>>,
    observation: (f64, f64),
    rejected_commands: u64,
    last_rejection: Option<String>,
}
impl M3508Device {
    pub fn new(options: DeviceOptions) -> Result<Self> {
        options.validate()?;
        Ok(Self {
            options,
            closed: false,
            session_id: None,
            command_raw: 0,
            command_expiry_ns: 0,
            time_ns: 0,
            step_id: 0,
            next_feedback_ns: 0,
            pending: None,
            observation: (0.0, 0.0),
            rejected_commands: 0,
            last_rejection: None,
        })
    }
    pub fn options(&self) -> &DeviceOptions {
        &self.options
    }
    pub fn is_closed(&self) -> bool {
        self.closed
    }
    pub fn time_ns(&self) -> u64 {
        self.time_ns
    }
    pub fn rotor_state(&self) -> (f64, f64) {
        self.observation
    }
    pub fn current_raw(&self) -> i16 {
        if u128::from(self.time_ns) < self.command_expiry_ns {
            self.command_raw
        } else {
            0
        }
    }
    pub fn current_amp(&self) -> f64 {
        f64::from(self.current_raw()) * (20.0 / 16384.0)
    }
    pub fn state(&self) -> DeviceState {
        DeviceState {
            current_raw: self.current_raw(),
            rejected_commands: self.rejected_commands,
        }
    }
    pub fn last_rejection(&self) -> Option<&str> {
        self.last_rejection.as_deref()
    }
    fn check(&self) -> Result<()> {
        if self.closed || self.session_id.is_none() {
            Err(state_error(
                "M3508 requires a successful reset and open state",
            ))
        } else {
            Ok(())
        }
    }
    pub fn reset(&mut self, session: Session, position: f64, velocity: f64) -> Result<()> {
        if self.closed {
            return Err(state_error("M3508 is closed"));
        }
        Session::new(session.session_id, session.t_sim_ns)?;
        finite(position, velocity)?;
        self.session_id = Some(session.session_id);
        self.time_ns = session.t_sim_ns;
        self.step_id = 0;
        self.command_raw = 0;
        self.command_expiry_ns = u128::from(session.t_sim_ns);
        self.next_feedback_ns =
            u128::from(session.t_sim_ns) + u128::from(self.options.feedback_period_ns);
        self.pending = None;
        self.observation = (position, velocity);
        self.rejected_commands = 0;
        self.last_rejection = None;
        Ok(())
    }
    pub fn receive(&mut self, frame: &CanFrame) -> Result<()> {
        let raw = match decode_current_command(frame, self.options.motor_id) {
            Ok(Some(value)) => value,
            Ok(None) => return Ok(()),
            Err(error) => {
                self.rejected_commands = self
                    .rejected_commands
                    .checked_add(1)
                    .ok_or_else(|| state_error("M3508 rejection count exhausted"))?;
                self.last_rejection = Some(error.to_string());
                return Ok(());
            }
        };
        self.check()?;
        if self.pending.is_some() {
            return Err(state_error(
                "M3508 command cannot change during a prepared step",
            ));
        }
        if frame.timestamp_ns() != self.time_ns {
            return Err(admission(
                "M3508 command must be delivered at current device time",
            ));
        }
        self.command_raw = raw;
        self.command_expiry_ns =
            u128::from(frame.timestamp_ns()) + u128::from(self.options.command_timeout_ns);
        Ok(())
    }
    pub fn feedback(&self) -> Result<CanFrame> {
        self.feedback_for(self.observation.0, self.observation.1)
    }
    fn feedback_for(&self, position: f64, velocity: f64) -> Result<CanFrame> {
        finite(position, velocity)?;
        let encoder = ((position.rem_euclid(TAU) / TAU * 8192.0) as u64 % 8192) as u16;
        let rpm = if velocity >= 32767.0 * PI / 30.0 {
            32767
        } else if velocity <= -32768.0 * PI / 30.0 {
            -32768
        } else {
            (velocity * 30.0 / PI).round_ties_even() as i16
        };
        encode_feedback(
            self.options.motor_id,
            encoder,
            rpm,
            self.current_raw(),
            self.options.temperature_c,
            self.time_ns,
        )
        .map_err(|e| -> Failure { e })
    }
    pub fn prepare_step(&mut self, context: StepContext) -> Result<DrivePlan> {
        self.check()?;
        if self.pending.is_some() {
            return Err(state_error("M3508 already has a prepared step"));
        }
        StepContext::new(
            context.session_id,
            context.step_id,
            context.t_sim_ns,
            context.dt_ns,
        )?;
        if Some(context.session_id) != self.session_id
            || self.step_id.checked_add(1) != Some(context.step_id)
            || self.time_ns.checked_add(context.dt_ns) != Some(context.t_sim_ns)
        {
            return Err(admission(
                "M3508 step must follow its session and exact target",
            ));
        }
        let active_until_ns = if self.command_raw != 0 {
            u128::from(context.t_sim_ns)
                .min(self.command_expiry_ns)
                .max(u128::from(self.time_ns)) as u64
        } else {
            self.time_ns
        };
        let effort_nm = if active_until_ns > self.time_ns {
            f64::from(self.command_raw) * (20.0 / 16384.0) * self.options.torque_per_amp
        } else {
            0.0
        };
        let identity = Rc::new(());
        self.pending = Some(identity.clone());
        Ok(DrivePlan {
            identity,
            context,
            active_until_ns,
            effort_nm,
        })
    }
    pub fn complete_step(
        &mut self,
        plan: &DrivePlan,
        position: f64,
        velocity: f64,
        emit: &mut impl FnMut(CanFrame) -> Result<()>,
    ) -> Result<StepResult<DeviceState>> {
        self.check()?;
        if !self
            .pending
            .as_ref()
            .is_some_and(|pending| Rc::ptr_eq(pending, &plan.identity))
        {
            return Err(admission(
                "M3508 completion requires its original pending plan",
            ));
        }
        finite(position, velocity)?;
        self.observation = (position, velocity);
        self.time_ns = plan.context.t_sim_ns;
        self.step_id = plan.context.step_id;
        self.pending = None;
        if u128::from(self.time_ns) >= self.next_feedback_ns {
            emit(self.feedback()?)?;
            let period = u128::from(self.options.feedback_period_ns);
            self.next_feedback_ns +=
                ((u128::from(self.time_ns) - self.next_feedback_ns) / period + 1) * period;
        }
        Ok(StepResult {
            status: sim_core::StepStatus::Ok,
            reached_time_ns: self.time_ns,
            diagnostics: Some(self.state()),
        })
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.pending = None;
    }
}

/// A standalone toy rotor consumer. A shared native world instead consumes the
/// same device plans and advances all motors together before completing any.
pub struct M3508 {
    pub device: M3508Device,
    pub rotor: Rotor,
}
impl M3508 {
    pub fn new(options: DeviceOptions, rotor: Rotor) -> Result<Self> {
        Ok(Self {
            device: M3508Device::new(options)?,
            rotor,
        })
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        let (position, velocity) = self.rotor.initial_state();
        self.device.reset(session, position, velocity)?;
        self.rotor.reset();
        Ok(())
    }
    pub fn feedback(&self) -> Result<CanFrame> {
        let (position, velocity) = self.rotor.state();
        self.device.feedback_for(position, velocity)
    }
    pub fn step(
        &mut self,
        context: StepContext,
        emit: &mut impl FnMut(CanFrame) -> Result<()>,
    ) -> Result<StepResult<DeviceState>> {
        let plan = self.device.prepare_step(context)?;
        let active = plan.active_until_ns - (context.t_sim_ns - context.dt_ns);
        if active > 0 {
            self.rotor.step(active as f64 / 1e9, plan.effort_nm)?;
        }
        let coast = context.t_sim_ns - plan.active_until_ns;
        if coast > 0 {
            self.rotor.step(coast as f64 / 1e9, 0.0)?;
        }
        let (position, velocity) = self.rotor.state();
        self.device.complete_step(&plan, position, velocity, emit)
    }
}
