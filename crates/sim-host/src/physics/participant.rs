//! Own one native world, with session progress separate from native seconds.

use super::{JointPhysics, NativePhysics};
use sim_core::{Error, Failure, Result, Session, StepContext, StepResult};
use std::sync::Arc;

pub(crate) struct Progress {
    session: Option<u64>,
    step_id: u64,
    pub time_ns: u64,
    failed: bool,
    closed: bool,
}
impl Default for Progress {
    fn default() -> Self {
        Self {
            session: None,
            step_id: 0,
            time_ns: 0,
            failed: true,
            closed: false,
        }
    }
}
impl Progress {
    pub fn open(&self) -> Result<()> {
        if self.closed {
            return Err(Arc::new(Error::State("physics execution is closed".into())));
        }
        Ok(())
    }
    pub fn live(&self) -> Result<()> {
        self.open()?;
        if self.failed || self.session.is_none() {
            return Err(Arc::new(Error::State(
                "physics execution requires a successful reset".into(),
            )));
        }
        Ok(())
    }
    pub fn resetting(&mut self, session: Session) -> Result<()> {
        self.open()?;
        Session::new(session.session_id, session.t_sim_ns)?;
        self.failed = true;
        Ok(())
    }
    pub fn reset(&mut self, session: Session) {
        self.session = Some(session.session_id);
        self.step_id = 0;
        self.time_ns = session.t_sim_ns;
        self.failed = false;
    }
    pub fn stepping(&mut self, context: StepContext) -> Result<()> {
        self.live()?;
        StepContext::new(
            context.session_id,
            context.step_id,
            context.t_sim_ns,
            context.dt_ns,
        )?;
        if self.session != Some(context.session_id)
            || self.step_id.checked_add(1) != Some(context.step_id)
            || self.time_ns.checked_add(context.dt_ns) != Some(context.t_sim_ns)
            || context.dt_ns > i64::MAX as u64
        {
            return Err(Arc::new(Error::Admission(
                "physics step must follow its session and exact target".into(),
            )));
        }
        self.failed = true;
        Ok(())
    }
    pub fn commit(&mut self, context: StepContext) {
        self.step_id = context.step_id;
        self.time_ns = context.t_sim_ns;
        self.failed = false;
    }
    #[cfg(feature = "devices")]
    pub fn freeze(&mut self) {
        self.failed = true;
    }
    pub fn close(&mut self) -> bool {
        let was_open = !self.closed;
        self.closed = true;
        self.failed = true;
        was_open
    }
}

pub struct PhysicsParticipant<P: NativePhysics> {
    physics: P,
    progress: Progress,
}
impl<P: NativePhysics> PhysicsParticipant<P> {
    pub fn new(mut physics: P) -> Result<Self> {
        if let Err(error) = native_time(&physics) {
            return Err(close_rejected(&mut physics, error));
        }
        Ok(Self {
            physics,
            progress: Progress::default(),
        })
    }
    pub fn physics(&self) -> &P {
        &self.physics
    }
    #[cfg(all(feature = "simulation", any(feature = "mujoco", feature = "gazebo")))]
    pub(crate) fn physics_mut(&mut self) -> &mut P {
        &mut self.physics
    }
    pub fn time_ns(&self) -> u64 {
        self.progress.time_ns
    }
    pub fn check_live(&self) -> Result<()> {
        self.progress.live()
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        self.progress.resetting(session)?;
        self.physics.reset()?;
        if native_time(&self.physics)? != 0.0 {
            return Err(Arc::new(Error::State(
                "physics reset did not restore native time zero".into(),
            )));
        }
        self.progress.reset(session);
        Ok(())
    }
    pub fn step(&mut self, context: StepContext) -> Result<StepResult<f64>> {
        self.progress.stepping(context)?;
        self.physics.validate_step(context.dt_ns)?;
        self.physics.step(context.dt_ns)?;
        let time = native_time(&self.physics)?;
        self.progress.commit(context);
        Ok(StepResult {
            status: sim_core::StepStatus::Ok,
            reached_time_ns: context.t_sim_ns,
            diagnostics: Some(time),
        })
    }
    pub fn close(&mut self) -> Result<()> {
        if self.progress.close() {
            self.physics.close()?;
        }
        Ok(())
    }
}

/// Construction already owns the native resource, including rejected input.
/// Keep the primary as the source when independent cleanup also fails.
pub(crate) fn close_rejected(physics: &mut impl JointPhysics, primary: Failure) -> Failure {
    #[derive(Debug)]
    struct CleanupFailure {
        primary: Failure,
        cleanup: Failure,
    }
    impl std::fmt::Display for CleanupFailure {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(
                f,
                "{}; native physics cleanup also failed: {}",
                self.primary, self.cleanup
            )
        }
    }
    impl std::error::Error for CleanupFailure {
        fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
            Some(self.primary.as_ref())
        }
    }
    match physics.close() {
        Ok(()) => primary,
        Err(cleanup) => Arc::new(CleanupFailure { primary, cleanup }),
    }
}
pub(crate) fn native_time(physics: &impl NativePhysics) -> Result<f64> {
    let time = physics.time_s()?;
    if !time.is_finite() || time < 0.0 {
        return Err(Arc::new(Error::State(
            "native physics time must be finite nonnegative seconds".into(),
        )));
    }
    Ok(time)
}
