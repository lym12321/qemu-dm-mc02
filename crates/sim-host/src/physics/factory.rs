//! Compiled native-world and C620 consumers. The world is the only native
//! reset/step/close owner; bound motors retain commands and CAN feedback.

use super::{ImuSample, JointState};

#[derive(Clone, Debug, PartialEq)]
pub struct PhysicsState {
    pub native_time_s: f64,
    pub joints: Vec<(String, JointState)>,
    pub imus: Vec<(String, ImuSample)>,
}

#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
use super::binding::JointDriveBinding;
#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
use crate::runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry};
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
use sim_core::Direction;
#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
use sim_core::{
    BuildContext, Capability, Component, Error, Factory, Failure, NodeDescription, NodeSpec,
    PortCategory, PortSpec, Result, Session, StepContext, StepResult, Stepper, Topology,
};
#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
use std::cell::RefCell;
#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
use std::{collections::BTreeSet, rc::Rc, sync::Arc};

#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}
#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
fn binding_port(name: &str, protocol: &str) -> PortSpec {
    let mut port = PortSpec::new(name, protocol);
    port.category = PortCategory::Binding;
    port
}

#[cfg(any(feature = "mujoco", feature = "gazebo"))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorldControl {
    Direct,
    Bound,
}

#[cfg(feature = "mujoco")]
#[derive(Clone, Debug)]
pub struct MujocoOptions {
    pub model: std::path::PathBuf,
    pub bindings: super::mujoco::MujocoBindings,
    pub control: WorldControl,
}
#[cfg(feature = "mujoco")]
impl MujocoOptions {
    pub fn validate(&self) -> Result<()> {
        self.bindings.validate()?;
        for (alias, _, _) in &self.bindings.imus {
            if alias.contains('.') {
                return Err(admission(
                    "world IMU aliases require a single port identifier",
                ));
            }
            sim_core::Endpoint::new("world", format!("imu.{alias}"))?;
        }
        if self.model.as_os_str().is_empty()
            || self.model.to_str().is_none_or(|path| path.contains('\0'))
            || !self.model.is_file()
            || self.bindings.joints.is_empty() && self.bindings.imus.is_empty()
            || self.control == WorldControl::Bound && self.bindings.joints.is_empty()
        {
            return Err(admission("MuJoCo requires a model file and joint/IMU bindings; bound control requires joints"));
        }
        Ok(())
    }
}

#[cfg(feature = "gazebo")]
#[derive(Clone, Debug)]
pub struct GazeboOptions {
    pub model: std::path::PathBuf,
    pub bindings: super::gazebo::GazeboBindings,
    pub resource_paths: Vec<std::path::PathBuf>,
    pub control: WorldControl,
}
#[cfg(feature = "gazebo")]
impl GazeboOptions {
    pub fn validate(&self) -> Result<()> {
        self.bindings.validate()?;
        for (alias, _, _, _) in &self.bindings.imus {
            if alias.contains('.') {
                return Err(admission(
                    "world IMU aliases require a single port identifier",
                ));
            }
            sim_core::Endpoint::new("world", format!("imu.{alias}"))?;
        }
        if self.model.as_os_str().is_empty()
            || self.model.to_str().is_none_or(|path| path.contains('\0'))
            || !self.model.is_file()
            || self.control == WorldControl::Bound && self.bindings.joints.is_empty()
        {
            return Err(admission(
                "Gazebo requires a model file; bound control requires joints",
            ));
        }
        super::gazebo::resource_directories(&self.resource_paths)?;
        Ok(())
    }
}

#[cfg(any(feature = "mujoco", feature = "gazebo"))]
enum NativeOptions<'a> {
    #[cfg(feature = "mujoco")]
    Mujoco(&'a MujocoOptions),
    #[cfg(feature = "gazebo")]
    Gazebo(&'a GazeboOptions),
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl<'a> NativeOptions<'a> {
    fn from_node(node: &'a NodeSpec<HostOptions>) -> Result<Self> {
        match (node.kind.as_str(), &node.options) {
            #[cfg(feature = "mujoco")]
            ("physics.mujoco", HostOptions::Mujoco(options)) => Ok(Self::Mujoco(options)),
            #[cfg(feature = "gazebo")]
            ("physics.gazebo", HostOptions::Gazebo(options)) => Ok(Self::Gazebo(options)),
            _ => Err(admission(
                "native world requires options matching its backend kind",
            )),
        }
    }
    fn validate(&self) -> Result<()> {
        match self {
            #[cfg(feature = "mujoco")]
            Self::Mujoco(options) => options.validate(),
            #[cfg(feature = "gazebo")]
            Self::Gazebo(options) => options.validate(),
        }
    }
    fn control(&self) -> WorldControl {
        match self {
            #[cfg(feature = "mujoco")]
            Self::Mujoco(options) => options.control,
            #[cfg(feature = "gazebo")]
            Self::Gazebo(options) => options.control,
        }
    }
    fn joint_names(&self) -> Vec<String> {
        match self {
            #[cfg(feature = "mujoco")]
            Self::Mujoco(options) => options
                .bindings
                .joints
                .iter()
                .map(|(name, _)| name.clone())
                .collect(),
            #[cfg(feature = "gazebo")]
            Self::Gazebo(options) => options
                .bindings
                .joints
                .iter()
                .map(|(name, _, _)| name.clone())
                .collect(),
        }
    }
    fn imu_names(&self) -> Vec<String> {
        match self {
            #[cfg(feature = "mujoco")]
            Self::Mujoco(options) => options
                .bindings
                .imus
                .iter()
                .map(|(name, _, _)| name.clone())
                .collect(),
            #[cfg(feature = "gazebo")]
            Self::Gazebo(options) => options
                .bindings
                .imus
                .iter()
                .map(|(name, _, _, _)| name.clone())
                .collect(),
        }
    }
    fn acquire(&self) -> Result<Box<dyn NativePhysics>> {
        match self {
            #[cfg(feature = "mujoco")]
            Self::Mujoco(options) => Ok(Box::new(super::mujoco::MujocoPhysics::from_xml_path(
                &options.model,
                options.bindings.clone(),
            )?)),
            #[cfg(feature = "gazebo")]
            Self::Gazebo(options) => Ok(Box::new(super::gazebo::GazeboPhysics::from_sdf_path(
                &options.model,
                options.bindings.clone(),
                &options.resource_paths,
            )?)),
        }
    }
}

#[cfg(any(feature = "mujoco", feature = "gazebo"))]
use super::{joints::JointExecution, participant::PhysicsParticipant, JointPhysics, NativePhysics};
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
enum WorldExecution {
    Unbound(Box<dyn NativePhysics>),
    Direct(PhysicsParticipant<Box<dyn NativePhysics>>),
    Bound(JointExecution<Box<dyn NativePhysics>>),
    Closed,
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
struct WorldState {
    execution: WorldExecution,
    completed_cut: Option<u64>,
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
pub struct NativeWorld {
    state: Rc<RefCell<WorldState>>,
    context: HostContext,
    joint_names: Vec<String>,
    imu_names: Vec<String>,
    bound: bool,
    bindings: Vec<JointDriveBinding>,
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl WorldState {
    fn physics(&self) -> Result<&dyn NativePhysics> {
        match &self.execution {
            WorldExecution::Unbound(physics) => Ok(physics.as_ref()),
            WorldExecution::Direct(owner) => Ok(owner.physics().as_ref()),
            WorldExecution::Bound(owner) => Ok(owner.physics().as_ref()),
            WorldExecution::Closed => Err(state_error("native world is closed")),
        }
    }
    fn completed(&self) -> Result<u64> {
        let cut = self
            .completed_cut
            .ok_or_else(|| state_error("world reads require a completed healthy cut"))?;
        match &self.execution {
            WorldExecution::Direct(owner) => owner.check_live()?,
            WorldExecution::Bound(owner) => owner.check_live()?,
            _ => return Err(state_error("world requires an initialized executor")),
        }
        Ok(cut)
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl NativeWorld {
    fn with_completed<T>(
        &self,
        operation: impl FnOnce(&dyn NativePhysics, u64) -> Result<T>,
    ) -> Result<T> {
        self.context.check_io()?;
        let result = (|| {
            let state = self
                .state
                .try_borrow()
                .map_err(|_| state_error("native world reads require completed execution"))?;
            operation(state.physics()?, state.completed()?)
        })();
        result.map_err(|error| {
            if matches!(error.downcast_ref::<Error>(), Some(Error::Admission(_))) {
                error
            } else {
                self.context.fail(error)
            }
        })
    }
    pub fn time_s(&self) -> Result<f64> {
        self.with_completed(|physics, _| physics.time_s())
    }
    pub fn get_joint_state(&self, name: &str) -> Result<JointState> {
        self.with_completed(|physics, _| physics.get_joint_state(name))
    }
    pub fn get_body_pose(&self, name: &str) -> Result<super::BodyPose> {
        self.with_completed(|physics, _| physics.get_body_pose(name))
    }
    pub fn get_imu(&self, name: &str) -> Result<ImuSample> {
        self.with_completed(|physics, _| physics.get_imu(name))
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl sim_core::ConnectionOwner<HostInput, HostEvent> for NativeWorld {
    fn attach_port(
        &mut self,
        attachment: sim_core::Attachment<HostInput, HostEvent>,
    ) -> Result<Box<dyn std::any::Any>> {
        if !self.bound
            || !matches!(self.state.borrow().execution, WorldExecution::Unbound(_))
            || attachment.owner_port != "drives"
            || attachment.descriptor.category != PortCategory::Binding
            || attachment.descriptor.protocol != "joint-drive"
            || attachment.descriptor.direction != Direction::InOut
        {
            return Err(admission(
                "world requires an uninitialized joint-drive binding",
            ));
        }
        let binding = attachment
            .endpoint
            .downcast::<JointDriveBinding>()
            .map_err(|_| admission("world requires a typed JointDriveBinding"))?;
        if !self.joint_names.contains(&binding.joint)
            || self.bindings.iter().any(|previous| {
                previous.joint == binding.joint || Rc::ptr_eq(&previous.driver, &binding.driver)
            })
        {
            return Err(admission(
                "world joints and drivers require distinct declared bindings",
            ));
        }
        self.bindings.push(*binding);
        Ok(Box::new(()))
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl Stepper<HostInput, HostEvent> for NativeWorld {
    fn step(&mut self, step: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        let mut state = self.state.borrow_mut();
        state.completed_cut = None;
        match &mut state.execution {
            WorldExecution::Direct(owner) => {
                owner.step(*step)?;
            }
            WorldExecution::Bound(owner) => {
                owner.step(*step)?;
            }
            _ => return Err(state_error("world requires an initialized executor")),
        }
        state.completed_cut = Some(step.t_sim_ns);
        Ok(StepResult::reached(step.t_sim_ns))
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
fn efforts(names: &[String], values: &[(String, f64)]) -> Result<()> {
    let mut seen = BTreeSet::new();
    if values.is_empty()
        || values
            .iter()
            .any(|(name, value)| !names.contains(name) || !value.is_finite() || !seen.insert(name))
    {
        return Err(admission(
            "joint effort requires distinct bound joints and finite torque values",
        ));
    }
    Ok(())
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl sim_core::Input<HostInput, HostEvent> for NativeWorld {
    fn input(&mut self, port: &str, input: HostInput, _: &HostContext) -> Result<()> {
        if self.bound || port != "effort" {
            return Err(admission("world cannot accept effort input"));
        }
        let HostInput::JointEffort(values) = input else {
            return Err(admission("world effort requires joint torque values"));
        };
        efforts(&self.joint_names, &values)?;
        let mut state = self.state.borrow_mut();
        let WorldExecution::Direct(owner) = &mut state.execution else {
            return Err(state_error("world requires direct executor"));
        };
        for (name, effort) in &values {
            owner.physics().validate_joint_effort(name, *effort)?;
        }
        for (name, effort) in values {
            owner.physics_mut().set_joint_effort(&name, effort)?;
        }
        Ok(())
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl sim_core::Sampler<HostInput, HostEvent> for NativeWorld {
    fn sample(&mut self, cut: u64, context: &HostContext) -> Result<()> {
        let state = self.with_completed(|physics, completed| {
            if completed != cut {
                return Err(state_error("world sample requires its completed cut"));
            }
            Ok(PhysicsState {
                native_time_s: physics.time_s()?,
                joints: self
                    .joint_names
                    .iter()
                    .map(|name| Ok((name.clone(), physics.get_joint_state(name)?)))
                    .collect::<Result<_>>()?,
                imus: self
                    .imu_names
                    .iter()
                    .map(|name| Ok((name.clone(), physics.get_imu(name)?)))
                    .collect::<Result<_>>()?,
            })
        })?;
        for (_, sample) in &state.imus {
            sample.validate()?;
        }
        context.emit(
            Some("state"),
            "physics.state",
            HostEvent::PhysicsState(state),
            sim_core::EventTime {
                mapped_time_ns: Some(cut),
                delivery_time_ns: Some(cut),
                quality: Some(sim_core::TimeQuality::Sampled),
                ..Default::default()
            },
        )?;
        Ok(())
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl Component<HostInput, HostEvent> for NativeWorld {
    fn close(&mut self) -> Result<()> {
        let mut state = self.state.borrow_mut();
        state.completed_cut = None;
        match std::mem::replace(&mut state.execution, WorldExecution::Closed) {
            WorldExecution::Unbound(mut physics) => physics.close(),
            WorldExecution::Direct(mut owner) => owner.close(),
            WorldExecution::Bound(mut owner) => owner.close(),
            WorldExecution::Closed => Ok(()),
        }
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        let mut state = self.state.borrow_mut();
        state.completed_cut = None;
        state.execution = match std::mem::replace(&mut state.execution, WorldExecution::Closed) {
            WorldExecution::Unbound(physics) => {
                if self.bound {
                    WorldExecution::Bound(JointExecution::new(physics, self.bindings.clone())?)
                } else {
                    WorldExecution::Direct(PhysicsParticipant::new(physics)?)
                }
            }
            existing => existing,
        };
        match &mut state.execution {
            WorldExecution::Direct(owner) => owner.reset(session)?,
            WorldExecution::Bound(owner) => owner.reset(session)?,
            _ => return Err(state_error("world executor is unavailable during reset")),
        }
        state.completed_cut = Some(session.t_sim_ns);
        Ok(())
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        Some(self)
    }
    fn sampler(&mut self) -> Option<&mut dyn sim_core::Sampler<HostInput, HostEvent>> {
        Some(self)
    }
    fn input(&mut self) -> Option<&mut dyn sim_core::Input<HostInput, HostEvent>> {
        (!self.bound && !self.joint_names.is_empty()).then_some(self)
    }
    fn connection_owner(
        &mut self,
    ) -> Option<&mut dyn sim_core::ConnectionOwner<HostInput, HostEvent>> {
        self.bound.then_some(self)
    }
}

#[cfg(any(feature = "mujoco", feature = "gazebo"))]
pub struct NativeFactory;
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl sim_core::InputValidator<HostOptions, HostInput> for NativeFactory {
    fn validate_input(
        &self,
        node: &NodeSpec<HostOptions>,
        port: &str,
        value: HostInput,
        connected: &[String],
    ) -> Result<HostInput> {
        let options = NativeOptions::from_node(node)?;
        let HostInput::JointEffort(values) = &value else {
            return Err(admission("world effort requires joint torque values"));
        };
        if options.control() != WorldControl::Direct
            || port != "effort"
            || connected.iter().any(|name| name == port)
        {
            return Err(admission("world requires unconnected direct effort input"));
        }
        efforts(&options.joint_names(), values)?;
        Ok(value)
    }
}
#[cfg(any(feature = "mujoco", feature = "gazebo"))]
impl Factory<HostOptions, HostInput, HostEvent> for NativeFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let options = NativeOptions::from_node(node)?;
        options.validate()?;
        let mut state = binding_port("state", "joint-state");
        state.direction = Direction::Out;
        let mut ports = vec![state];
        for alias in options.imu_names() {
            let mut imu = PortSpec::new(format!("imu.{alias}"), "imu");
            imu.category = PortCategory::Signal;
            imu.direction = Direction::Out;
            ports.push(imu);
        }
        let mut capabilities = vec![Capability::Step, Capability::Sample];
        if options.control() == WorldControl::Bound {
            let mut drives = binding_port("drives", "joint-drive");
            drives.min_connections = 1;
            drives.max_connections = None;
            ports.push(drives);
            capabilities.push(Capability::BindingOwner);
        } else if !options.joint_names().is_empty() {
            let mut effort = binding_port("effort", "joint-effort");
            effort.direction = Direction::In;
            effort.capabilities.insert("external_input".into());
            ports.push(effort);
            capabilities.push(Capability::Input);
        }
        Ok(NodeDescription::new(ports, capabilities))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let options = NativeOptions::from_node(node)?;
        let physics = options.acquire()?;
        let world = NativeWorld {
            state: Rc::new(RefCell::new(WorldState {
                execution: WorldExecution::Unbound(physics),
                completed_cut: None,
            })),
            context: build.context(),
            joint_names: options.joint_names(),
            imu_names: options.imu_names(),
            bound: options.control() == WorldControl::Bound,
            bindings: vec![],
        };
        for alias in &world.imu_names {
            let port = format!("imu.{alias}");
            if build.peers(&port)?.is_empty() {
                continue;
            }
            let state = Rc::downgrade(&world.state);
            let context = world.context.clone();
            let alias = alias.clone();
            build.attach::<_, ()>(
                &port,
                crate::fabric::imu::ImuAttachment::Source(Rc::new(move || {
                    context.check_io()?;
                    let result = (|| {
                        let owner = state
                            .upgrade()
                            .ok_or_else(|| state_error("native world is closed"))?;
                        let state = owner.try_borrow().map_err(|_| {
                            state_error("native world reads require completed execution")
                        })?;
                        let cut = state.completed()?;
                        Ok(crate::signals::ImuSignalSample {
                            sample: state.physics()?.get_imu(&alias)?,
                            source_time_ns: None,
                            mapped_time_ns: cut,
                        })
                    })();
                    result.map_err(|error| context.fail(error))
                })),
            )?;
        }
        Ok(Box::new(world))
    }
    fn input_validator(&self) -> Option<&dyn sim_core::InputValidator<HostOptions, HostInput>> {
        Some(self)
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        _: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        let mut occupied: BTreeSet<(String, String)> = BTreeSet::new();
        for link in &topology.links {
            for (endpoint, peer) in [(&link.source, &link.target), (&link.target, &link.source)] {
                let node = topology
                    .nodes
                    .iter()
                    .find(|node| node.name == endpoint.node)
                    .expect("core admitted node");
                if !matches!(node.kind.as_str(), "physics.mujoco" | "physics.gazebo") {
                    continue;
                }
                let options = NativeOptions::from_node(node)?;
                if options
                    .imu_names()
                    .iter()
                    .any(|alias| endpoint.port == format!("imu.{alias}"))
                {
                    continue;
                }
                if options.control() != WorldControl::Bound || endpoint.port != "drives" {
                    return Err(admission("world connections require bound joint drives"));
                }
                #[cfg(feature = "devices")]
                {
                    let peer_node = topology
                        .nodes
                        .iter()
                        .find(|node| node.name == peer.node)
                        .expect("core admitted peer");
                    let HostOptions::M3508(M3508Options {
                        plant: MotorPlant::Joint { joint, .. },
                        ..
                    }) = &peer_node.options
                    else {
                        return Err(admission(
                            "world drives require a described joint motor binding",
                        ));
                    };
                    if peer.port != "joint"
                        || !options.joint_names().contains(joint)
                        || !occupied.insert((node.name.clone(), joint.clone()))
                    {
                        return Err(admission(
                            "world joint bindings must be declared and distinct",
                        ));
                    }
                }
                #[cfg(not(feature = "devices"))]
                {
                    let _ = (&mut occupied, peer);
                    return Err(admission(
                        "world joint bindings require the devices feature",
                    ));
                }
            }
        }
        Ok(())
    }
}

#[cfg(feature = "devices")]
#[derive(Clone, Debug)]
pub enum MotorPlant {
    FixedRotor {
        position_rad: f64,
        velocity_rad_s: f64,
    },
    RigidRotor(super::rotor::RotorOptions),
    Joint {
        joint: String,
        gear_ratio: f64,
    },
}
#[cfg(feature = "devices")]
#[derive(Clone, Debug)]
pub struct M3508Options {
    pub device: crate::devices::dji::DeviceOptions,
    pub plant: MotorPlant,
}
#[cfg(feature = "devices")]
impl M3508Options {
    pub fn validate(&self) -> Result<()> {
        self.device.validate()?;
        match &self.plant {
            MotorPlant::FixedRotor {
                position_rad,
                velocity_rad_s,
            } if !position_rad.is_finite() || !velocity_rad_s.is_finite() => {
                return Err(admission("fixed rotor values must be finite"))
            }
            MotorPlant::RigidRotor(options) => {
                super::rotor::Rotor::new(*options)?;
            }
            MotorPlant::Joint { joint, gear_ratio }
                if joint.is_empty()
                    || joint.contains('\0')
                    || !gear_ratio.is_finite()
                    || *gear_ratio <= 0.0
                    || !(20.0 * self.device.torque_per_amp * gear_ratio).is_finite() =>
            {
                return Err(admission(
                    "joint motor requires a name and finite positive gearing/maximum effort",
                ))
            }
            _ => {}
        }
        Ok(())
    }
}

#[cfg(feature = "devices")]
enum MotorExecution {
    Fixed(f64, f64),
    Rigid(super::rotor::Rotor),
    Bound,
}
#[cfg(feature = "devices")]
pub(crate) struct Motor {
    device: Rc<RefCell<crate::devices::dji::M3508Device>>,
    execution: MotorExecution,
    connection: Rc<RefCell<crate::runtime::CanConnection>>,
}
#[cfg(all(feature = "devices", feature = "worker"))]
impl Motor {
    pub(crate) fn device_state(&self) -> Result<crate::devices::dji::DeviceState> {
        Ok(self
            .device
            .try_borrow()
            .map_err(|_| state_error("motor reads require completed execution"))?
            .state())
    }
}
#[cfg(feature = "devices")]
impl Component<HostInput, HostEvent> for Motor {
    fn close(&mut self) -> Result<()> {
        self.device.borrow_mut().close();
        Ok(())
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        let (position, velocity) = match &mut self.execution {
            MotorExecution::Fixed(position, velocity) => (*position, *velocity),
            MotorExecution::Rigid(rotor) => {
                rotor.reset();
                rotor.state()
            }
            MotorExecution::Bound => return Ok(()),
        };
        self.device.borrow_mut().reset(session, position, velocity)
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        (!matches!(self.execution, MotorExecution::Bound)).then_some(self)
    }
}
#[cfg(feature = "devices")]
impl Stepper<HostInput, HostEvent> for Motor {
    fn step(&mut self, context: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        let mut device = self.device.borrow_mut();
        let plan = device.prepare_step(*context)?;
        let (position, velocity) = match &mut self.execution {
            MotorExecution::Fixed(position, velocity) => (*position, *velocity),
            MotorExecution::Rigid(rotor) => {
                let active = plan.active_until_ns() - (context.t_sim_ns - context.dt_ns);
                if active > 0 {
                    rotor.step(active as f64 / 1e9, plan.effort_nm())?;
                }
                let coast = context.t_sim_ns - plan.active_until_ns();
                if coast > 0 {
                    rotor.step(coast as f64 / 1e9, 0.0)?;
                }
                rotor.state()
            }
            MotorExecution::Bound => {
                return Err(admission("bound motors are stepped by their world"))
            }
        };
        let result = device.complete_step(&plan, position, velocity, &mut |frame| {
            self.connection.borrow_mut().send(frame)
        })?;
        Ok(StepResult {
            status: result.status,
            reached_time_ns: result.reached_time_ns,
            diagnostics: result.diagnostics.map(|state| HostEvent::M3508State {
                current_raw: state.current_raw,
                rejected_commands: state.rejected_commands,
            }),
        })
    }
}

#[cfg(feature = "devices")]
pub struct M3508Factory;
#[cfg(feature = "devices")]
impl Factory<HostOptions, HostInput, HostEvent> for M3508Factory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::M3508(options) = &node.options else {
            return Err(admission("device.dji-m3508 requires M3508 options"));
        };
        options.validate()?;
        let mut can = PortSpec::new("can", "can");
        can.min_connections = 1;
        if matches!(options.plant, MotorPlant::Joint { .. }) {
            let mut joint = binding_port("joint", "joint-drive");
            joint.min_connections = 1;
            Ok(NodeDescription::new(vec![can, joint], []))
        } else {
            Ok(NodeDescription::new(vec![can], [Capability::Step]))
        }
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::M3508(options) = &node.options else {
            return Err(admission("device.dji-m3508 requires M3508 options"));
        };
        let device = Rc::new(RefCell::new(crate::devices::dji::M3508Device::new(
            options.device.clone(),
        )?));
        let receiver = device.clone();
        let context = build.context();
        let connection = build.attach(
            "can",
            crate::runtime::CanAttachment {
                endpoint: crate::can::EndpointId::new(),
                receive: Rc::new(move |frame, _| {
                    receiver
                        .borrow_mut()
                        .receive(frame)
                        .map_err(|error| context.fail(error))
                }),
            },
        )?;
        let connection = Rc::new(RefCell::new(connection));
        let execution = match &options.plant {
            MotorPlant::FixedRotor {
                position_rad,
                velocity_rad_s,
            } => MotorExecution::Fixed(*position_rad, *velocity_rad_s),
            MotorPlant::RigidRotor(options) => {
                MotorExecution::Rigid(super::rotor::Rotor::new(*options)?)
            }
            MotorPlant::Joint { joint, gear_ratio } => {
                let send: Rc<RefCell<crate::runtime::CanConnection>> = connection.clone();
                let driver = crate::devices::dji::joints::M3508JointDriver::new(
                    device.clone(),
                    *gear_ratio,
                    Box::new(move |frame| send.borrow_mut().send(frame)),
                )?;
                build.attach::<_, ()>(
                    "joint",
                    JointDriveBinding::new(joint, Rc::new(RefCell::new(driver)))?,
                )?;
                MotorExecution::Bound
            }
        };
        Ok(Box::new(Motor {
            device,
            execution,
            connection,
        }))
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        _: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        let mut identities = BTreeSet::new();
        for node in &topology.nodes {
            let HostOptions::M3508(options) = &node.options else {
                continue;
            };
            for link in &topology.links {
                let peer = if link.source.node == node.name && link.source.port == "can" {
                    Some(&link.target)
                } else if link.target.node == node.name && link.target.port == "can" {
                    Some(&link.source)
                } else {
                    None
                };
                if let Some(peer) = peer {
                    if !identities.insert((peer.node.clone(), options.device.motor_id)) {
                        return Err(admission("M3508 motor IDs must be unique on each CAN bus"));
                    }
                }
            }
        }
        Ok(())
    }
}
