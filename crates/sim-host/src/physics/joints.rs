//! One world execution owner: prepare all, union expiries, write all torques,
//! advance once per interval, validate all target samples, then feedback.

use super::{
    binding::{validate_sample, JointDriveBinding},
    participant::{close_rejected, Progress},
    JointPhysics, JointState,
};
use sim_core::{Error, Result, Session, StepContext, StepResult, StepStatus};
use std::{collections::BTreeSet, rc::Rc, sync::Arc};

pub struct JointExecution<P: JointPhysics> {
    physics: P,
    bindings: Vec<JointDriveBinding>,
    maxima: Vec<f64>,
    progress: Progress,
}
impl<P: JointPhysics> JointExecution<P> {
    /// Transfers exclusive native execution, closing rejected construction.
    /// Drivers remain borrowed; close never closes them or their CAN buses.
    pub fn new(mut physics: P, bindings: Vec<JointDriveBinding>) -> Result<Self> {
        let maxima = match Self::admit(&physics, &bindings) {
            Ok(maxima) => maxima,
            Err(error) => return Err(close_rejected(&mut physics, error)),
        };
        let mut execution = Self {
            physics,
            bindings,
            maxima,
            progress: Progress::default(),
        };
        if let Err(error) = execution.samples() {
            return Err(close_rejected(&mut execution.physics, error));
        }
        Ok(execution)
    }
    fn admit(physics: &P, bindings: &[JointDriveBinding]) -> Result<Vec<f64>> {
        let mut joints = BTreeSet::new();
        let mut drivers = BTreeSet::new();
        if bindings.is_empty()
            || bindings.iter().any(|binding| {
                binding.joint.is_empty()
                    || binding.joint.contains('\0')
                    || !joints.insert(&binding.joint)
                    || !drivers.insert(Rc::as_ptr(&binding.driver) as *const () as usize)
            })
        {
            return Err(Arc::new(Error::Admission(
                "each world joint and driver requires one distinct nonempty binding".into(),
            )));
        }
        let mut maxima = Vec::new();
        for binding in bindings {
            let maximum = binding.driver.borrow().max_abs_effort_nm();
            if !maximum.is_finite() || maximum < 0.0 {
                return Err(Arc::new(Error::Admission(
                    "maximum joint effort must be finite and nonnegative".into(),
                )));
            }
            maxima.push(maximum);
            for effort in [maximum, -maximum, 0.0] {
                physics.validate_joint_effort(&binding.joint, effort)?;
            }
        }
        Ok(maxima)
    }
    pub fn physics(&self) -> &P {
        &self.physics
    }
    pub fn time_ns(&self) -> u64 {
        self.progress.time_ns
    }
    pub fn check_live(&self) -> Result<()> {
        self.progress.live()
    }
    #[cfg(feature = "devices")]
    pub(crate) fn fail(&mut self, error: sim_core::Failure) -> sim_core::Failure {
        self.progress.freeze();
        error
    }
    fn samples(&self) -> Result<Vec<JointState>> {
        self.bindings
            .iter()
            .map(|binding| {
                let state = self.physics.get_joint_state(&binding.joint)?;
                validate_sample(state)?;
                binding.driver.borrow().validate_joint_state(state)?;
                Ok(state)
            })
            .collect()
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        self.progress.resetting(session)?;
        self.physics.reset()?;
        let samples = self.samples()?;
        for (binding, state) in self.bindings.iter().zip(samples) {
            binding.driver.borrow_mut().reset(session, state)?;
        }
        self.progress.reset(session);
        Ok(())
    }
    pub fn step(&mut self, context: StepContext) -> Result<StepResult<usize>> {
        self.progress.stepping(context)?;
        let samples = self.samples()?;
        let plans = self
            .bindings
            .iter()
            .zip(samples)
            .map(|(binding, state)| binding.driver.borrow_mut().prepare_step(context, state))
            .collect::<Result<Vec<_>>>()?;
        for (plan, maximum) in plans.iter().zip(&self.maxima) {
            if plan.context() != context || plan.effort_nm().abs() > *maximum {
                return Err(Arc::new(Error::Admission(
                    "joint plan must fit its exact step and admitted maximum effort".into(),
                )));
            }
        }
        let mut boundaries = BTreeSet::from([self.progress.time_ns, context.t_sim_ns]);
        boundaries.extend(plans.iter().map(|plan| plan.active_until_ns()));
        let boundaries = boundaries.into_iter().collect::<Vec<_>>();
        let intervals = boundaries
            .windows(2)
            .map(|pair| pair[1] - pair[0])
            .collect::<Vec<_>>();
        self.physics.validate_step(context.dt_ns)?;
        self.physics.validate_steps(&intervals)?;
        for (binding, plan) in self.bindings.iter().zip(&plans) {
            self.physics
                .validate_joint_effort(&binding.joint, plan.effort_nm())?;
            self.physics.validate_joint_effort(&binding.joint, 0.0)?;
        }
        for interval in boundaries.windows(2) {
            for (binding, plan) in self.bindings.iter().zip(&plans) {
                self.physics.set_joint_effort(
                    &binding.joint,
                    if interval[0] < plan.active_until_ns() {
                        plan.effort_nm()
                    } else {
                        0.0
                    },
                )?;
            }
            self.physics.step(interval[1] - interval[0])?;
        }
        let samples = self.samples()?;
        for ((binding, plan), state) in self.bindings.iter().zip(&plans).zip(samples) {
            let result = binding.driver.borrow_mut().complete_step(plan, state)?;
            if result.status != StepStatus::Ok || result.reached_time_ns != context.t_sim_ns {
                return Err(Arc::new(Error::State(
                    "joint completion must reach its exact target".into(),
                )));
            }
        }
        self.progress.commit(context);
        Ok(StepResult {
            status: StepStatus::Ok,
            reached_time_ns: context.t_sim_ns,
            diagnostics: Some(intervals.len()),
        })
    }
    pub fn close(&mut self) -> Result<()> {
        if self.progress.close() {
            self.physics.close()?;
        }
        Ok(())
    }
}
