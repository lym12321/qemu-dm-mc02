//! Existing core/C620/native-world factories composed for worker transport.
use crate::{
    can::{CanBusOptions, CanFrame},
    devices::dji::DeviceOptions,
    physics::{
        factory::{M3508Options, MotorPlant},
        rotor::RotorOptions,
    },
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
    signals::ImuSample,
};
use sim_core::{
    Endpoint, Error, EventPayload, Failure, LinkSpec, NodeSpec, Result, Session, SimulationOptions,
    Topology,
};
use std::{
    cell::RefCell,
    collections::BTreeSet,
    rc::Rc,
    sync::{atomic::AtomicBool, Arc},
    time::Instant,
};

#[derive(Clone, Debug)]
pub struct RotorProfile {
    pub motor_ids: Vec<u8>,
    pub delay_ns: u64,
    pub command_timeout_ns: u64,
    pub torque_per_amp: f64,
}
impl Default for RotorProfile {
    fn default() -> Self {
        Self {
            motor_ids: vec![1],
            delay_ns: 0,
            command_timeout_ns: 100_000_000,
            torque_per_amp: 0.02,
        }
    }
}
#[derive(Clone, Debug)]
pub enum CanProfile {
    Rotor(RotorProfile),
    #[cfg(feature = "mujoco")]
    FourWheel {
        model: PathBuf,
        delay_ns: u64,
        command_timeout_ns: u64,
        torque_per_amp: f64,
    },
}
#[cfg(feature = "mujoco")]
use std::path::PathBuf;
fn link(source: impl AsRef<str>, target: impl AsRef<str>) -> Result<LinkSpec> {
    Ok(LinkSpec::new(
        Endpoint::parse(source.as_ref())?,
        Endpoint::parse(target.as_ref())?,
    ))
}
fn admission(s: &str) -> Failure {
    Arc::new(Error::Admission(s.into()))
}
impl CanProfile {
    fn options(&self) -> RotorProfile {
        match self {
            Self::Rotor(o) => o.clone(),
            #[cfg(feature = "mujoco")]
            Self::FourWheel {
                delay_ns,
                command_timeout_ns,
                torque_per_amp,
                ..
            } => RotorProfile {
                motor_ids: vec![1, 2, 3, 4],
                delay_ns: *delay_ns,
                command_timeout_ns: *command_timeout_ns,
                torque_per_amp: *torque_per_amp,
            },
        }
    }
    pub fn validate(&self) -> Result<()> {
        let o = self.options();
        let mut ids = BTreeSet::new();
        if o.motor_ids.is_empty() || o.motor_ids.len() > 8 {
            return Err(admission("worker C620 needs1..8 unique motor IDs"));
        }
        for id in o.motor_ids {
            if !ids.insert(id) {
                return Err(admission("worker C620 IDs must be unique"));
            }
            DeviceOptions {
                motor_id: id,
                command_timeout_ns: o.command_timeout_ns,
                torque_per_amp: o.torque_per_amp,
                ..Default::default()
            }
            .validate()?;
        }
        #[cfg(feature = "mujoco")]
        if let Self::FourWheel { model, .. } = self {
            Self::model_digest(model)?;
        }
        Ok(())
    }
    #[cfg(feature = "mujoco")]
    fn model_digest(path: &std::path::Path) -> Result<[u8; 32]> {
        use sha2::{Digest, Sha256};
        use std::io::Read;
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .and_then(|f| f.take(1_048_577).read_to_end(&mut bytes))
            .map_err(|e| Arc::new(e) as Failure)?;
        if bytes.len() > 1_048_576 {
            return Err(admission("four-wheel MJCF exceeds1MiB"));
        }
        let text =
            std::str::from_utf8(&bytes).map_err(|_| admission("four-wheel MJCF must be UTF-8"))?;
        let xml = roxmltree::Document::parse(text)
            .map_err(|e| admission(&format!("four-wheel MJCF: {e}")))?;
        if xml
            .descendants()
            .any(|n| n.is_element() && matches!(n.tag_name().name(), "include" | "asset"))
        {
            return Err(admission(
                "four-wheel worker requires self-contained MJCF without includes/assets",
            ));
        }
        Ok(Sha256::digest(&bytes).into())
    }
    fn topology(&self, socketcan: bool) -> Result<Topology<HostOptions>> {
        self.validate()?;
        let o = self.options();
        let mut nodes = vec![
            NodeSpec::new(
                "can",
                "bus.can",
                HostOptions::CanBus(
                    CanBusOptions {
                        delay_ns: o.delay_ns,
                        ..Default::default()
                    }
                    .into(),
                ),
            ),
            NodeSpec::new("qemu", "host.can", HostOptions::CanTransportBridge),
        ];
        let mut links = vec![link("qemu.can", "can.can")?];
        if socketcan {
            nodes.push(NodeSpec::new(
                "socketcan",
                "host.can",
                HostOptions::CanTransportBridge,
            ));
            links.push(link("socketcan.can", "can.can")?);
        }
        for id in o.motor_ids {
            let name = format!("motor{id}");
            let plant = match self {
                Self::Rotor(_) => MotorPlant::RigidRotor(RotorOptions::default()),
                #[cfg(feature = "mujoco")]
                Self::FourWheel { .. } => MotorPlant::Joint {
                    joint: format!("wheel_{}", ["fl", "fr", "rl", "rr"][id as usize - 1]),
                    gear_ratio: crate::devices::dji::joints::M3508_P19_RATIO,
                },
            };
            nodes.push(NodeSpec::new(
                &name,
                "device.dji-m3508",
                HostOptions::M3508(M3508Options {
                    device: DeviceOptions {
                        motor_id: id,
                        command_timeout_ns: o.command_timeout_ns,
                        torque_per_amp: o.torque_per_amp,
                        ..Default::default()
                    },
                    plant,
                }),
            ));
            links.push(link(format!("{name}.can"), "can.can")?);
            #[cfg(feature = "mujoco")]
            if matches!(self, Self::FourWheel { .. }) {
                links.push(link(format!("{name}.joint"), "world.drives")?);
            }
        }
        #[cfg(feature = "mujoco")]
        if let Self::FourWheel { model, .. } = self {
            use crate::physics::{
                factory::{MujocoOptions, WorldControl},
                mujoco::MujocoBindings,
            };
            nodes.push(NodeSpec::new(
                "world",
                "physics.mujoco",
                HostOptions::Mujoco(MujocoOptions {
                    model: model.clone(),
                    control: WorldControl::Bound,
                    bindings: MujocoBindings {
                        joints: ["fl", "fr", "rl", "rr"]
                            .into_iter()
                            .map(|c| (format!("wheel_{c}"), format!("drive_{c}")))
                            .collect(),
                        imus: vec![("imu".into(), "imu_gyro".into(), "imu_accel".into())],
                    },
                }),
            ));
        }
        Topology::new(nodes, links)
    }
    pub fn admit(
        &self,
        socketcan: bool,
        deadline: Instant,
        cancellation: Option<Arc<AtomicBool>>,
    ) -> Result<CanAdmission> {
        self.validate()?;
        #[cfg(feature = "mujoco")]
        let digest = match self {
            Self::FourWheel { model, .. } => Some(Self::model_digest(model)?),
            _ => None,
        };
        let admission = CanAdmission {
            profile: self.clone(),
            #[cfg(feature = "mujoco")]
            digest,
        };
        // A complete actual binding gate, then independent cleanup, precedes
        // transport acquisition. Each acknowledged session gets a fresh graph.
        let mut preflight = CanGraph::new(
            &admission,
            socketcan,
            Session::new(1, 0)?,
            deadline,
            cancellation,
        )?;
        let result = preflight.sample().and_then(|sample| sample.validate());
        match (result, preflight.close()) {
            (Err(primary), Err(cleanup)) => {
                eprintln!("worker native preflight cleanup: {cleanup}");
                Err(primary)
            }
            (Err(error), _) | (_, Err(error)) => Err(error),
            (Ok(()), Ok(())) => Ok(admission),
        }
    }
}
pub struct CanAdmission {
    profile: CanProfile,
    #[cfg(feature = "mujoco")]
    digest: Option<[u8; 32]>,
}
pub struct CanGraph {
    simulation: HostSimulation,
    outputs: Rc<RefCell<Vec<(String, CanFrame)>>>,
    closed: bool,
}
/// Read-only view of one completed worker cut. It cannot drive or retain the
/// graph, its resources or its scheduler.
pub struct CanObservation<'a> {
    pub session: Session,
    pub step_id: u64,
    pub mapper: crate::endpoints::qemu_can::QemuTimestampMapper,
    graph: &'a CanGraph,
}
impl<'a> CanObservation<'a> {
    pub(crate) fn new(
        graph: &'a CanGraph,
        session: Session,
        step_id: u64,
        mapper: crate::endpoints::qemu_can::QemuTimestampMapper,
    ) -> Self {
        Self {
            graph,
            session,
            step_id,
            mapper,
        }
    }
    pub fn imu(&self) -> Result<ImuSample> {
        self.graph.sample()
    }
    pub fn motor_current_raw(&self, id: u8) -> Result<i16> {
        Ok(self.graph.motor_state(id)?.current_raw)
    }
    #[cfg(feature = "mujoco")]
    fn with_world<T>(
        &self,
        read: impl FnOnce(&crate::physics::factory::NativeWorld) -> Result<T>,
    ) -> Result<T> {
        self.graph.with_world(read)
    }
    #[cfg(feature = "mujoco")]
    pub fn native_time_s(&self) -> Result<f64> {
        self.with_world(|world| world.time_s())
    }
    #[cfg(feature = "mujoco")]
    pub fn body_pose(&self, name: &str) -> Result<crate::physics::BodyPose> {
        self.with_world(|world| world.get_body_pose(name))
    }
    #[cfg(feature = "mujoco")]
    pub fn joint_state(&self, name: &str) -> Result<crate::physics::JointState> {
        self.with_world(|world| world.get_joint_state(name))
    }
}
impl CanGraph {
    #[cfg(feature = "mujoco")]
    fn with_world<T>(
        &self,
        read: impl FnOnce(&crate::physics::factory::NativeWorld) -> Result<T>,
    ) -> Result<T> {
        let handle = self
            .simulation
            .handle::<crate::physics::factory::NativeWorld>("world")?;
        read(self.simulation.component(&handle)?)
    }
    /// The native world's completed clock, distinct from the session epoch.
    #[cfg(feature = "mujoco")]
    pub fn native_time_s(&self) -> Result<f64> {
        self.with_world(|world| world.time_s())
    }
    /// Completed protocol-device state; this grants no plant or bus access.
    pub fn motor_state(&self, id: u8) -> Result<crate::devices::dji::DeviceState> {
        let handle = self
            .simulation
            .handle::<crate::physics::factory::Motor>(&format!("motor{id}"))?;
        self.simulation.component(&handle)?.device_state()
    }
    pub fn new(
        admitted: &CanAdmission,
        socketcan: bool,
        session: Session,
        deadline: Instant,
        cancellation: Option<Arc<AtomicBool>>,
    ) -> Result<Self> {
        #[cfg(feature = "mujoco")]
        if let CanProfile::FourWheel { model, .. } = &admitted.profile {
            if Some(CanProfile::model_digest(model)?) != admitted.digest {
                return Err(admission("four-wheel model changed after native preflight"));
            }
        }
        let mut simulation = HostSimulation::new(
            admitted.profile.topology(socketcan)?,
            runtime::registry()?,
            SimulationOptions {
                initial_session: session,
                quantum_ns: 1_000_000,
                max_pending_inputs: 4096,
                event_history: 1,
                cancellation,
                ..Default::default()
            },
        )?;
        let outputs = Rc::new(RefCell::new(Vec::new()));
        let pending = outputs.clone();
        simulation.set_observer(Some(Rc::new(move |event, _| {
            if let EventPayload::Observation(HostEvent::CanHostReceived { frame }) = &event.payload
            {
                let mut pending = pending.borrow_mut();
                if pending.len() == 4096 {
                    return Err(Arc::new(Error::Capacity(
                        "worker CAN output queue overflow".into(),
                    )));
                }
                pending.push((event.source_node.clone(), frame.clone()));
            }
            Ok(())
        })));
        simulation.start(Some(deadline))?;
        Ok(Self {
            simulation,
            outputs,
            closed: false,
        })
    }
    pub fn submit(&self, source: &str, frame: CanFrame) -> Result<()> {
        self.simulation.schedule_input(
            Endpoint::new(source, "tx")?,
            HostInput::CanFrame(frame),
            None,
        )
    }
    pub fn reach(&mut self, target: u64, deadline: Instant) -> Result<()> {
        self.simulation.run_until(target, Some(deadline))?;
        Ok(())
    }
    pub fn sample(&self) -> Result<ImuSample> {
        #[cfg(feature = "mujoco")]
        if self
            .simulation
            .topology()
            .nodes
            .iter()
            .any(|n| n.name == "world")
        {
            let handle = self
                .simulation
                .handle::<crate::physics::factory::NativeWorld>("world")?;
            return self.simulation.component(&handle)?.get_imu("imu");
        }
        Ok(ImuSample {
            gyro_rad_s: [0.0; 3],
            accel_m_s2: [0.0, 0.0, 9.80665],
        })
    }
    pub fn take_outputs(&mut self) -> Result<Vec<(String, CanFrame)>> {
        if let Some(primary) = self.simulation.failure() {
            return Err(primary);
        }
        Ok(std::mem::take(&mut *self.outputs.borrow_mut()))
    }
    pub fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        self.simulation.close()
    }
}
impl Drop for CanGraph {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
