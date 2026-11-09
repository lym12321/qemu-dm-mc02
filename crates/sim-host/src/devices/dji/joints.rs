//! The sole ideal gearbox converter. DJI M3508 P19 manual V1.0 gives3591/187;
//! efficiency, backlash and rotor inertia remain uncalibrated approximations.

use super::{DrivePlan, M3508Device};
use crate::{
    can::CanFrame,
    physics::{
        binding::{validate_sample, JointDrivePlan, JointDriver},
        JointState,
    },
};
use sim_core::{Error, Result, Session, StepContext, StepResult};
use std::{cell::RefCell, rc::Rc, sync::Arc};

pub const M3508_P19_RATIO: f64 = 3591.0 / 187.0;
pub type FeedbackSink = Box<dyn FnMut(CanFrame) -> Result<()>>;

pub struct M3508JointDriver {
    device: Rc<RefCell<M3508Device>>,
    gear_ratio: f64,
    maximum: f64,
    pending: Option<(Rc<JointDrivePlan>, DrivePlan)>,
    emit: FeedbackSink,
}
impl M3508JointDriver {
    pub fn new(
        device: Rc<RefCell<M3508Device>>,
        gear_ratio: f64,
        emit: FeedbackSink,
    ) -> Result<Self> {
        let maximum = 20.0 * device.borrow().options().torque_per_amp * gear_ratio;
        if device.borrow().is_closed()
            || !gear_ratio.is_finite()
            || gear_ratio <= 0.0
            || !maximum.is_finite()
        {
            return Err(Arc::new(Error::Admission("joint driver requires an open device and finite positive gear ratio/maximum effort".into())));
        }
        Ok(Self {
            device,
            gear_ratio,
            maximum,
            pending: None,
            emit,
        })
    }
    fn rotor_sample(&self, state: JointState) -> Result<(f64, f64)> {
        validate_sample(state)?;
        let rotor = JointState {
            position_rad: state.position_rad * self.gear_ratio,
            velocity_rad_s: state.velocity_rad_s * self.gear_ratio,
        };
        validate_sample(rotor)?;
        Ok((rotor.position_rad, rotor.velocity_rad_s))
    }
}
impl JointDriver for M3508JointDriver {
    fn max_abs_effort_nm(&self) -> f64 {
        self.maximum
    }
    fn validate_joint_state(&self, state: JointState) -> Result<()> {
        self.rotor_sample(state).map(|_| ())
    }
    fn reset(&mut self, session: Session, initial: JointState) -> Result<()> {
        let (position, velocity) = self.rotor_sample(initial)?;
        self.pending = None;
        self.device.borrow_mut().reset(session, position, velocity)
    }
    fn prepare_step(
        &mut self,
        context: StepContext,
        initial: JointState,
    ) -> Result<Rc<JointDrivePlan>> {
        if self.pending.is_some() {
            return Err(Arc::new(Error::State(
                "M3508 joint driver already has a prepared step".into(),
            )));
        }
        self.validate_joint_state(initial)?;
        let raw = self.device.borrow_mut().prepare_step(context)?;
        let plan = Rc::new(JointDrivePlan::new(
            context,
            raw.active_until_ns(),
            raw.effort_nm() * self.gear_ratio,
        )?);
        self.pending = Some((plan.clone(), raw));
        Ok(plan)
    }
    fn complete_step(
        &mut self,
        plan: &JointDrivePlan,
        target: JointState,
    ) -> Result<StepResult<()>> {
        if !self
            .pending
            .as_ref()
            .is_some_and(|(pending, _)| std::ptr::eq(pending.as_ref(), plan))
        {
            return Err(Arc::new(Error::Admission(
                "M3508 joint completion requires its original prepared plan".into(),
            )));
        }
        let (position, velocity) = self.rotor_sample(target)?;
        let (_, raw) = self.pending.take().expect("checked original plan");
        // Native state/device completion may commit before feedback fails. Never replay.
        let result =
            self.device
                .borrow_mut()
                .complete_step(&raw, position, velocity, &mut self.emit)?;
        Ok(StepResult {
            status: result.status,
            reached_time_ns: result.reached_time_ns,
            diagnostics: None,
        })
    }
}

/// Explicit borrowed CAN identity and emission capability. The plant never
/// resets, steps, dispatches or closes that bus.
pub struct M3508PlantBinding {
    device: Rc<RefCell<M3508Device>>,
    bus: Arc<()>,
    joint: String,
    gear_ratio: f64,
    emit: FeedbackSink,
}
impl M3508PlantBinding {
    pub fn new(
        device: Rc<RefCell<M3508Device>>,
        bus: &crate::can::CanBus,
        joint: impl Into<String>,
        gear_ratio: f64,
        emit: FeedbackSink,
    ) -> Self {
        Self {
            device,
            bus: bus.identity().clone(),
            joint: joint.into(),
            gear_ratio,
            emit,
        }
    }
}

/// Owning composition for direct native worker consumers. JointExecution is
/// still the sole stepping algorithm; this composition adds only device and
/// native resource cleanup. Devices transfer on successful admission; rejected
/// construction leaves their external owners responsible for them.
pub struct M3508JointPlant<P: crate::physics::JointPhysics> {
    execution: crate::physics::joints::JointExecution<P>,
    devices: Vec<(Arc<()>, Rc<RefCell<M3508Device>>)>,
}
impl<P: crate::physics::JointPhysics> M3508JointPlant<P> {
    pub fn new(mut physics: P, bindings: Vec<M3508PlantBinding>) -> Result<Self> {
        use std::collections::BTreeSet;
        let mut identities = BTreeSet::new();
        let mut device_identities = BTreeSet::new();
        let mut devices = Vec::new();
        let drivers = bindings
            .into_iter()
            .map(|binding| {
                let identity = (
                    Arc::as_ptr(&binding.bus) as usize,
                    binding.device.borrow().options().motor_id,
                );
                if !identities.insert(identity)
                    || !device_identities.insert(Rc::as_ptr(&binding.device) as usize)
                {
                    return Err(Arc::new(Error::Admission(
                        "M3508 motor IDs must be unique on each borrowed CAN bus".into(),
                    )) as sim_core::Failure);
                }
                let driver = M3508JointDriver::new(
                    binding.device.clone(),
                    binding.gear_ratio,
                    binding.emit,
                )?;
                devices.push((binding.bus, binding.device));
                crate::physics::binding::JointDriveBinding::new(
                    binding.joint,
                    Rc::new(RefCell::new(driver)),
                )
            })
            .collect::<Result<Vec<_>>>();
        let drivers = match drivers {
            Ok(drivers) => drivers,
            Err(error) => {
                return Err(crate::physics::participant::close_rejected(
                    &mut physics,
                    error,
                ))
            }
        };
        let execution = crate::physics::joints::JointExecution::new(physics, drivers)?;
        Ok(Self { execution, devices })
    }
    pub fn devices(&self) -> impl ExactSizeIterator<Item = &Rc<RefCell<M3508Device>>> {
        self.devices.iter().map(|(_, device)| device)
    }
    pub fn time_ns(&self) -> u64 {
        self.execution.time_ns()
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        self.execution.reset(session)
    }
    pub fn step(&mut self, context: StepContext) -> Result<StepResult<usize>> {
        self.execution.step(context)
    }
    pub fn ingress(&mut self, bus: &crate::can::CanBus, frame: &CanFrame) -> Result<()> {
        self.execution.check_live()?;
        if !self
            .devices
            .iter()
            .any(|(identity, _)| Arc::ptr_eq(identity, bus.identity()))
        {
            return Err(Arc::new(Error::Admission(
                "CAN ingress bus is not bound to this plant".into(),
            )));
        }
        for (identity, device) in &self.devices {
            if Arc::ptr_eq(identity, bus.identity()) {
                device
                    .borrow_mut()
                    .receive(frame)
                    .map_err(|error| self.execution.fail(error))?;
            }
        }
        Ok(())
    }
    pub fn get_joint_state(&self, name: &str) -> Result<JointState> {
        self.execution.check_live()?;
        self.execution.physics().get_joint_state(name)
    }
    pub fn close(&mut self) -> Result<()> {
        for (_, device) in self.devices.iter().rev() {
            device.borrow_mut().close();
        }
        self.execution.close()
    }
}
impl<P: crate::physics::NativePhysics> M3508JointPlant<P> {
    pub fn time_s(&self) -> Result<f64> {
        self.execution.physics().time_s()
    }
    pub fn get_imu(&self, name: &str) -> Result<crate::physics::ImuSample> {
        self.execution.check_live()?;
        self.execution.physics().get_imu(name)
    }
}
