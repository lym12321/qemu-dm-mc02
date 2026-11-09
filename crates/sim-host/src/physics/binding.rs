//! Borrowed joint drivers. Commands/expiry/feedback belong to the driver;
//! the native world remains inaccessible to it.

use super::JointState;
use sim_core::{Error, Result, Session, StepContext, StepResult};
use std::{cell::RefCell, rc::Rc, sync::Arc};

pub struct JointDrivePlan {
    context: StepContext,
    active_until_ns: u64,
    effort_nm: f64,
}
impl JointDrivePlan {
    pub fn new(context: StepContext, active_until_ns: u64, effort_nm: f64) -> Result<Self> {
        StepContext::new(
            context.session_id,
            context.step_id,
            context.t_sim_ns,
            context.dt_ns,
        )?;
        if active_until_ns < context.t_sim_ns - context.dt_ns
            || active_until_ns > context.t_sim_ns
            || !effort_nm.is_finite()
        {
            return Err(Arc::new(Error::Admission(
                "joint plan requires a finite effort and expiry inside its exact interval".into(),
            )));
        }
        Ok(Self {
            context,
            active_until_ns,
            effort_nm,
        })
    }
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

/// An immutable Rc plan preserves its original identity across the executor
/// and driver. No native solver, world time or resource ownership is borrowed.
pub trait JointDriver {
    fn max_abs_effort_nm(&self) -> f64;
    fn validate_joint_state(&self, state: JointState) -> Result<()>;
    fn reset(&mut self, session: Session, initial: JointState) -> Result<()>;
    fn prepare_step(
        &mut self,
        context: StepContext,
        initial: JointState,
    ) -> Result<Rc<JointDrivePlan>>;
    fn complete_step(
        &mut self,
        plan: &JointDrivePlan,
        target: JointState,
    ) -> Result<StepResult<()>>;
}

#[derive(Clone)]
pub struct JointDriveBinding {
    pub joint: String,
    pub driver: Rc<RefCell<dyn JointDriver>>,
}
impl JointDriveBinding {
    pub fn new(joint: impl Into<String>, driver: Rc<RefCell<dyn JointDriver>>) -> Result<Self> {
        let joint = joint.into();
        if joint.is_empty() || joint.contains('\0') {
            return Err(Arc::new(Error::Admission(
                "joint binding requires a nonempty name without NUL".into(),
            )));
        }
        Ok(Self { joint, driver })
    }
}

pub(crate) fn validate_sample(state: JointState) -> Result<()> {
    if !state.position_rad.is_finite() || !state.velocity_rad_s.is_finite() {
        return Err(Arc::new(Error::State(
            "joint physics returned a nonfinite state".into(),
        )));
    }
    Ok(())
}
