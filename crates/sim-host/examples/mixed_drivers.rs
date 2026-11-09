//! One native world shared by a C620 and a fixed ideal torque fixture.
//! JointExecution owns all native reset/step/close operations.
use sim_core::{Error, Result, Session, StepContext, StepResult};
use sim_host::{
    can::{CanBus, CanBusOptions, CanCallbacks, CanFrame, EndpointId},
    devices::dji::{joints::M3508JointDriver, DeviceOptions, M3508Device},
    physics::{
        binding::{JointDriveBinding, JointDrivePlan, JointDriver},
        joints::JointExecution,
        mujoco::{MujocoBindings, MujocoPhysics},
        JointPhysics, JointState,
    },
};
use std::{cell::RefCell, path::PathBuf, rc::Rc, sync::Arc};

/// Scripted -0.4 Nm for the first 7 ms; no motor or electrical model.
#[derive(Default)]
struct IdealEffort {
    expiry_ns: u64,
    pending: Option<Rc<JointDrivePlan>>,
}
impl JointDriver for IdealEffort {
    fn max_abs_effort_nm(&self) -> f64 {
        1.0
    }
    fn validate_joint_state(&self, state: JointState) -> Result<()> {
        if !state.position_rad.is_finite() || !state.velocity_rad_s.is_finite() {
            return Err(Arc::new(Error::State(
                "ideal driver requires finite joint state".into(),
            )));
        }
        Ok(())
    }
    fn reset(&mut self, session: Session, initial: JointState) -> Result<()> {
        self.validate_joint_state(initial)?;
        self.expiry_ns = session.t_sim_ns.checked_add(7_000_000).ok_or_else(|| {
            Arc::new(Error::Admission("ideal expiry overflow".into())) as sim_core::Failure
        })?;
        self.pending = None;
        Ok(())
    }
    fn prepare_step(
        &mut self,
        context: StepContext,
        initial: JointState,
    ) -> Result<Rc<JointDrivePlan>> {
        self.validate_joint_state(initial)?;
        if self.pending.is_some() {
            return Err(Arc::new(Error::State(
                "ideal driver already has a pending plan".into(),
            )));
        }
        let plan = Rc::new(JointDrivePlan::new(
            context,
            self.expiry_ns
                .clamp(context.t_sim_ns - context.dt_ns, context.t_sim_ns),
            -0.4,
        )?);
        self.pending = Some(plan.clone());
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
            .is_some_and(|pending| std::ptr::eq(pending.as_ref(), plan))
        {
            return Err(Arc::new(Error::Admission(
                "ideal completion requires its original plan".into(),
            )));
        }
        self.validate_joint_state(target)?;
        self.pending = None;
        Ok(StepResult::reached(plan.context().t_sim_ns))
    }
}
#[derive(Default)]
struct Receivers(Vec<CanFrame>);
impl CanCallbacks for Receivers {
    fn receive(
        &mut self,
        _: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> sim_host::Result<()> {
        self.0.push(frame.clone());
        Ok(())
    }
}
fn lower(error: sim_host::Failure) -> sim_core::Failure {
    error
}
fn main() -> Result<()> {
    let device = Rc::new(RefCell::new(M3508Device::new(DeviceOptions {
        command_timeout_ns: 3_000_000,
        ..Default::default()
    })?));
    let mut bus = CanBus::new(CanBusOptions::default()).map_err(lower)?;
    let (host, motor) = (EndpointId::new(), EndpointId::new());
    bus.attach(&host, "host").map_err(lower)?;
    bus.attach(&motor, "motor").map_err(lower)?;
    let bus = Rc::new(RefCell::new(bus));
    let feedback_bus = bus.clone();
    let dji = M3508JointDriver::new(
        device.clone(),
        2.0,
        Box::new(move |frame| {
            feedback_bus
                .borrow_mut()
                .send(&motor, frame, None)
                .map_err(|error| error as sim_core::Failure)
        }),
    )?;
    let bindings = vec![
        JointDriveBinding::new("j1", Rc::new(RefCell::new(dji)))?,
        JointDriveBinding::new("j2", Rc::new(RefCell::new(IdealEffort::default())))?,
    ];
    let model =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/shared-world/world.xml");
    let physics = MujocoPhysics::from_xml_path(
        model,
        MujocoBindings {
            joints: vec![("j1".into(), "m1".into()), ("j2".into(), "m2".into())],
            imus: vec![],
        },
    )?;
    let mut world = JointExecution::new(physics, bindings)?;
    let mut receivers = Receivers::default();
    let execution: Result<_> = (|| {
        bus.borrow_mut().reset(0).map_err(lower)?;
        world.reset(Session::new(1, 0)?)?;
        bus.borrow_mut()
            .send(
                &host,
                CanFrame::new(0x200, 0, None, [0x10, 0, 0, 0, 0, 0, 0, 0], 0).map_err(lower)?,
                None,
            )
            .map_err(lower)?;
        bus.borrow_mut()
            .step(0, None, &mut receivers)
            .map_err(lower)?;
        for frame in receivers.0.drain(..) {
            device.borrow_mut().receive(&frame)?;
        }
        world.step(StepContext::new(1, 1, 10_000_000, 10_000_000)?)?;
        bus.borrow_mut()
            .step(10_000_000, None, &mut receivers)
            .map_err(lower)?;
        Ok((
            world.physics().time_s()?,
            world.physics().get_joint_state("j1")?,
            world.physics().get_joint_state("j2")?,
        ))
    })();
    let cleanup = world.close();
    device.borrow_mut().close();
    let bus_cleanup = bus.borrow_mut().close();
    if let Err(error) = &cleanup {
        eprintln!("world cleanup: {error}");
    }
    if let Err(error) = &bus_cleanup {
        eprintln!("CAN cleanup: {error}");
    }
    let (native_time_s, j1, j2) = execution?;
    cleanup?;
    bus_cleanup.map_err(lower)?;
    println!("native_time_s={native_time_s}, j1={j1:?}, j2={j2:?}");
    println!("feedback={:?}", receivers.0);
    Ok(())
}
