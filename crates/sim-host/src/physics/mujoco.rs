//! MuJoCo3.12.0 through the mature mujoco-rs C API wrappers and resource owners.
//! Complete binding admission precedes MjData allocation and forward projection.
//! Every successful native step refreshes target pose/sensors with mj_forward;
//! getters only read that completed state.

use super::{BodyPose, ImuSample, JointPhysics, JointState};
use mujoco_rs::{prelude::*, wrappers::fun::utility::mju_is_bad};
use sim_core::{Error, Failure, Result};
use std::{
    cell::Cell,
    collections::{BTreeMap, BTreeSet},
    path::Path,
    sync::Arc,
};

pub use mujoco_rs::wrappers::MjModel as CompiledModel;

fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}
fn native(error: impl std::fmt::Display) -> Failure {
    state_error(format!("MuJoCo: {error}"))
}
fn name(value: &str) -> Result<()> {
    if value.is_empty() || value.contains('\0') {
        return Err(admission("MuJoCo names must be nonempty without NUL"));
    }
    Ok(())
}

#[derive(Clone, Debug, Default)]
pub struct MujocoBindings {
    pub joints: Vec<(String, String)>,
    pub imus: Vec<(String, String, String)>,
}
impl MujocoBindings {
    /// Pure shape/name admission before compiling a model or constructing data.
    pub fn validate(&self) -> Result<()> {
        let (mut joints, mut actuators, mut imus) =
            (BTreeSet::new(), BTreeSet::new(), BTreeSet::new());
        for (joint, actuator) in &self.joints {
            name(joint)?;
            name(actuator)?;
            if !joints.insert(joint) || !actuators.insert(actuator) {
                return Err(admission(
                    "MuJoCo joint and actuator bindings must be distinct",
                ));
            }
        }
        for (alias, gyro, accel) in &self.imus {
            name(alias)?;
            name(gyro)?;
            name(accel)?;
            if !imus.insert(alias) {
                return Err(admission("MuJoCo IMU aliases must be distinct"));
            }
        }
        Ok(())
    }
}
#[derive(Clone, Copy)]
struct JointBinding {
    position: usize,
    velocity: usize,
    control: usize,
}
#[derive(Clone, Copy)]
struct ImuBinding {
    gyro: usize,
    accel: usize,
}

pub struct MujocoPhysics {
    data: Option<MjData<Box<MjModel>>>,
    joints: BTreeMap<String, JointBinding>,
    imus: BTreeMap<String, ImuBinding>,
    failed: Cell<bool>,
}
impl MujocoPhysics {
    fn version() -> Result<()> {
        if mujoco_rs::mujoco_version() != "3.12.0" {
            return Err(Arc::new(Error::BackendUnavailable(
                "MuJoCo adapter requires native3.12.0".into(),
            )));
        }
        Ok(())
    }
    pub fn from_xml_path(path: impl AsRef<Path>, bindings: MujocoBindings) -> Result<Self> {
        bindings.validate()?;
        let path = path
            .as_ref()
            .to_str()
            .ok_or_else(|| admission("MuJoCo model path must be UTF-8"))?;
        name(path)?;
        Self::version()?;
        Self::from_model(MjModel::from_xml(path).map_err(native)?, bindings)
    }
    pub fn from_xml_string(xml: &str, bindings: MujocoBindings) -> Result<Self> {
        bindings.validate()?;
        if xml.contains('\0') {
            return Err(admission("MuJoCo XML must not contain NUL"));
        }
        Self::version()?;
        Self::from_model(MjModel::from_xml_string(xml).map_err(native)?, bindings)
    }
    /// Transfers the compiled model to the sole data owner. Construction
    /// rejection drops the model without creating runtime data or forwarding.
    pub fn from_model(model: MjModel, bindings: MujocoBindings) -> Result<Self> {
        Self::version()?;
        bindings.validate()?;
        if !model.opt().timestep.is_finite() || model.opt().timestep <= 0.0 {
            return Err(admission("MuJoCo timestep must be finite and positive"));
        }
        let mut joints = BTreeMap::new();
        for (joint, actuator) in bindings.joints {
            let j = Self::id(&model, MjtObj::mjOBJ_JOINT, &joint)?;
            let a = Self::id(&model, MjtObj::mjOBJ_ACTUATOR, &actuator)?;
            joints.insert(joint.clone(), Self::bind_joint(&model, j, a, &joint)?);
        }
        let mut imus = BTreeMap::new();
        for (alias, gyro, accel) in bindings.imus {
            let gyro = Self::id(&model, MjtObj::mjOBJ_SENSOR, &gyro)?;
            let accel = Self::id(&model, MjtObj::mjOBJ_SENSOR, &accel)?;
            imus.insert(alias.clone(), Self::bind_imu(&model, gyro, accel, &alias)?);
        }
        let mut data = MjData::try_new(Box::new(model)).map_err(native)?;
        data.forward();
        let physics = Self {
            data: Some(data),
            joints,
            imus,
            failed: Cell::new(false),
        };
        physics.check_solver()?;
        Ok(physics)
    }
    fn id(model: &MjModel, kind: MjtObj, value: &str) -> Result<usize> {
        name(value)?;
        model
            .name_to_id(kind, value)
            .ok_or_else(|| admission(format!("unknown MuJoCo name: {value}")))
    }
    fn bind_joint(model: &MjModel, j: usize, a: usize, name: &str) -> Result<JointBinding> {
        let c = usize::try_from(model.actuator_ctrladr()[a]).ok();
        let o = usize::try_from(model.actuator_outadr()[a]).ok();
        let group = model.actuator_group()[a];
        let supported = model.jnt_type()[j] == MjtJoint::mjJNT_HINGE
            && model.actuator_trntype()[a] == MjtTrn::mjTRN_JOINT
            && model.actuator_trnid()[a][0] == j as i32
            && model.actuator_ctrlnum()[a] == 1
            && model.actuator_outnum()[a] == 1
            && c.is_some_and(|c| c < model.nu() as usize)
            && o.is_some_and(|o| o < model.nout() as usize)
            && model.actuator_gear()[o.unwrap()] == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            && model.actuator_dyntype()[a] == MjtDyn::mjDYN_NONE
            && model.actuator_actnum()[a] == 0
            && model.actuator_actadr()[a] == -1
            && model.actuator_gaintype()[a] == MjtGain::mjGAIN_FIXED
            && model.actuator_gainprm()[a][0] == 1.0
            && model.actuator_biastype()[a] == MjtBias::mjBIAS_NONE
            && !model.actuator_ctrllimited()[c.unwrap()]
            && !model.actuator_forcelimited()[a]
            && !model.jnt_actfrclimited()[j]
            && !model.jnt_actgravcomp()[j]
            && model.actuator_history()[a] == [0, 0]
            && model.actuator_delay()[a] == 0.0
            && model.actuator_historyadr()[a] == -1
            && model.actuator_plugin()[a] == -1
            && model.opt().disableflags & MjtDisableBit::mjDSBL_ACTUATION as i32 == 0
            && !((0..=30).contains(&group) && model.opt().disableactuator & (1 << group) != 0);
        if !supported {
            return Err(admission(format!(
                "joint {name}: requires an immediate unclamped unit-gear hinge motor"
            )));
        }
        let position = usize::try_from(model.jnt_qposadr()[j])
            .ok()
            .filter(|value| *value < model.nq() as usize);
        let velocity = usize::try_from(model.jnt_dofadr()[j])
            .ok()
            .filter(|value| *value < model.nv() as usize);
        match (position, velocity, c) {
            (Some(position), Some(velocity), Some(control)) => Ok(JointBinding {
                position,
                velocity,
                control,
            }),
            _ => Err(admission(
                "MuJoCo joint state address is outside the compiled model",
            )),
        }
    }
    fn bind_imu(model: &MjModel, gyro: usize, accel: usize, name: &str) -> Result<ImuBinding> {
        for (sensor, kind) in [
            (gyro, MjtSensor::mjSENS_GYRO),
            (accel, MjtSensor::mjSENS_ACCELEROMETER),
        ] {
            if !(model.sensor_type()[sensor] == kind
                && model.sensor_dim()[sensor] == 3
                && model.sensor_objtype()[sensor] == MjtObj::mjOBJ_SITE
                && model.sensor_cutoff()[sensor] == 0.0
                && model.sensor_history()[sensor] == [0, 0]
                && model.sensor_historyadr()[sensor] == -1
                && model.sensor_delay()[sensor] == 0.0
                && model.sensor_interval()[sensor] == [0.0, 0.0]
                && model.sensor_noise()[sensor] == 0.0
                && model.sensor_plugin()[sensor] == -1
                && model.opt().disableflags & MjtDisableBit::mjDSBL_SENSOR as i32 == 0)
            {
                return Err(admission(format!(
                    "IMU {name}: requires immediate native site gyro/accelerometer"
                )));
            }
        }
        if model.sensor_objid()[gyro] != model.sensor_objid()[accel] {
            return Err(admission(format!(
                "IMU {name}: gyro and accelerometer must share a site"
            )));
        }
        let address = |sensor: usize| {
            usize::try_from(model.sensor_adr()[sensor])
                .ok()
                .filter(|value| {
                    value
                        .checked_add(3)
                        .is_some_and(|end| end <= model.nsensordata() as usize)
                })
        };
        match (address(gyro), address(accel)) {
            (Some(gyro), Some(accel)) => Ok(ImuBinding { gyro, accel }),
            _ => Err(admission(
                "MuJoCo IMU address is outside the compiled model",
            )),
        }
    }
    fn open(&self) -> Result<&MjData<Box<MjModel>>> {
        self.data
            .as_ref()
            .ok_or_else(|| state_error("MuJoCo physics is closed"))
    }
    fn live(&self) -> Result<&MjData<Box<MjModel>>> {
        let data = self.open()?;
        if self.failed.get() {
            return Err(state_error("MuJoCo solver failed; reset required"));
        }
        Ok(data)
    }
    fn check_values(&self, label: &str, values: impl IntoIterator<Item = f64>) -> Result<()> {
        if values.into_iter().any(mju_is_bad) {
            self.failed.set(true);
            return Err(state_error(format!(
                "MuJoCo invalid target {label}; reset required"
            )));
        }
        Ok(())
    }
    fn check_solver(&self) -> Result<()> {
        let data = self.open()?;
        for warning in [
            MjtWarning::mjWARN_BADCTRL,
            MjtWarning::mjWARN_BADQPOS,
            MjtWarning::mjWARN_BADQVEL,
            MjtWarning::mjWARN_BADQACC,
        ] {
            if data.warning()[warning as usize].number != 0 {
                self.failed.set(true);
                return Err(state_error(format!(
                    "MuJoCo solver warning {warning:?}; reset required"
                )));
            }
        }
        for (label, values) in [
            ("qpos", data.qpos()),
            ("qvel", data.qvel()),
            ("qacc", data.qacc()),
            ("sensordata", data.sensordata()),
        ] {
            self.check_values(label, values.iter().copied())?;
        }
        Ok(())
    }
    /// Native floating seconds, readable for failure diagnostics and independent
    /// of the Simulation/session integer epoch.
    pub fn time_s(&self) -> Result<f64> {
        Ok(self.open()?.time())
    }
    fn dt_seconds(&self, dt: f64, time_s: f64) -> Result<f64> {
        self.live()?;
        let target = time_s + dt;
        if !dt.is_finite() || dt <= 0.0 || !target.is_finite() || target <= time_s {
            return Err(admission(
                "MuJoCo step dt must be finite, positive and advance native time",
            ));
        }
        Ok(dt)
    }
    fn dt_ns(&self, dt_ns: u64, time_s: f64) -> Result<f64> {
        self.live()?;
        if dt_ns == 0 || dt_ns > i64::MAX as u64 {
            return Err(admission(
                "MuJoCo dt_ns must be a positive signed-64-bit integer",
            ));
        }
        self.dt_seconds(dt_ns as f64 / 1_000_000_000.0, time_s)
    }
    fn advance(&mut self, dt: f64) -> Result<()> {
        self.failed.set(true);
        let data = self.data.as_mut().expect("step admission checked open");
        data.model_opt_mut().timestep = dt;
        data.step();
        self.check_solver()?;
        self.data.as_mut().expect("owned data").forward();
        self.check_solver()?;
        self.failed.set(false);
        Ok(())
    }
    /// Preserve the legacy floating interval without a nanosecond round trip.
    pub fn step_seconds(&mut self, dt: f64) -> Result<()> {
        self.advance(self.dt_seconds(dt, self.time_s()?)?)
    }
    pub fn get_body_pose(&self, name: &str) -> Result<BodyPose> {
        let data = self.live()?;
        let body = Self::id(data.model(), MjtObj::mjOBJ_BODY, name)?;
        let pose = BodyPose {
            position_m: data.xpos()[body],
            quaternion_wxyz: data.xquat()[body],
        };
        self.check_values(
            "body pose",
            pose.position_m.into_iter().chain(pose.quaternion_wxyz),
        )?;
        Ok(pose)
    }
    pub fn get_imu(&self, name: &str) -> Result<ImuSample> {
        let data = self.live()?;
        let binding = self
            .imus
            .get(name)
            .ok_or_else(|| admission(format!("unbound IMU: {name}")))?;
        let sample = ImuSample {
            gyro_rad_s: data.sensordata()[binding.gyro..binding.gyro + 3]
                .try_into()
                .expect("admitted IMU dimension"),
            accel_m_s2: data.sensordata()[binding.accel..binding.accel + 3]
                .try_into()
                .expect("admitted IMU dimension"),
        };
        sample.validate()?;
        Ok(sample)
    }
    fn effort(&self, name: &str, effort: f64) -> Result<usize> {
        self.live()?;
        let binding = self
            .joints
            .get(name)
            .ok_or_else(|| admission(format!("unbound joint: {name}")))?;
        if !effort.is_finite() {
            return Err(admission("joint effort must be finite"));
        }
        Ok(binding.control)
    }
}
impl JointPhysics for MujocoPhysics {
    fn reset(&mut self) -> Result<()> {
        self.open()?;
        self.failed.set(true);
        let data = self.data.as_mut().expect("checked open");
        data.reset();
        data.forward();
        self.check_solver()?;
        self.failed.set(false);
        Ok(())
    }
    fn validate_step(&self, dt_ns: u64) -> Result<()> {
        self.dt_ns(dt_ns, self.time_s()?).map(|_| ())
    }
    fn validate_steps(&self, intervals: &[u64]) -> Result<()> {
        self.live()?;
        if intervals.is_empty() {
            return Err(admission("step intervals must be nonempty"));
        }
        let mut target = self.time_s()?;
        for interval in intervals {
            target += self.dt_ns(*interval, target)?;
        }
        Ok(())
    }
    fn step(&mut self, dt_ns: u64) -> Result<()> {
        self.advance(self.dt_ns(dt_ns, self.time_s()?)?)
    }
    fn validate_joint_effort(&self, name: &str, effort: f64) -> Result<()> {
        self.effort(name, effort).map(|_| ())
    }
    fn set_joint_effort(&mut self, name: &str, effort: f64) -> Result<()> {
        let control = self.effort(name, effort)?;
        self.data.as_mut().expect("effort checked open").ctrl_mut()[control] = effort;
        Ok(())
    }
    fn get_joint_state(&self, name: &str) -> Result<JointState> {
        let data = self.live()?;
        let binding = self
            .joints
            .get(name)
            .ok_or_else(|| admission(format!("unbound joint: {name}")))?;
        Ok(JointState {
            position_rad: data.qpos()[binding.position],
            velocity_rad_s: data.qvel()[binding.velocity],
        })
    }
    fn close(&mut self) -> Result<()> {
        self.data.take();
        Ok(())
    }
}

impl super::NativePhysics for MujocoPhysics {
    fn time_s(&self) -> Result<f64> {
        self.time_s()
    }
    fn get_body_pose(&self, name: &str) -> Result<BodyPose> {
        self.get_body_pose(name)
    }
    fn get_imu(&self, name: &str) -> Result<ImuSample> {
        self.get_imu(name)
    }
}
