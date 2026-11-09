//! Legacy indexed motor consumer of the existing native physics owners.
use super::{
    motor_policy::MotorPolicy,
    motor_protocol::{MotorAction, MotorCommand},
};
#[cfg(feature = "mujoco")]
use crate::physics::JointPhysics;
use crate::{
    physics::rotor::{Rotor, RotorOptions},
    signals::ImuSample,
};
use sim_core::{Error, Result};
#[cfg(feature = "mujoco")]
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Clone, Debug)]
pub enum EngineOptions {
    Null,
    #[cfg(feature = "mujoco")]
    Mujoco {
        model: PathBuf,
    },
    #[cfg(feature = "ros2")]
    Ros2 {
        options: crate::ros2::Ros2Options,
        wait_for_imu: bool,
        reset_timeout: std::time::Duration,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct EngineSample {
    pub imu: ImuSample,
    pub source_time_ns: Option<u64>,
    pub motors: Vec<(f64, f64, f64)>,
}
pub trait WorkerEngine {
    fn reset(&mut self) -> Result<()>;
    fn command(&mut self, command: MotorCommand) -> Result<()>;
    fn step(&mut self, dt: f64) -> Result<EngineSample>;
    fn close(&mut self) -> Result<()>;
}
fn admission(message: &str) -> sim_core::Failure {
    Arc::new(Error::Admission(message.into()))
}

impl EngineOptions {
    pub fn validate(&self, count: usize, dt: f64, limit: f64) -> Result<()> {
        if count > 1024 || !dt.is_finite() || dt <= 0.0 {
            return Err(admission(
                "worker needs bounded motors and finite positive native dt",
            ));
        }
        MotorPolicy::new(limit)?;
        match self {
            Self::Null => {}
            #[cfg(feature = "mujoco")]
            Self::Mujoco { model } => {
                if !model.is_file() {
                    return Err(admission("worker MuJoCo model must be a file"));
                }
            }
            #[cfg(feature = "ros2")]
            Self::Ros2 {
                options,
                reset_timeout,
                ..
            } => {
                options.validate().map_err(|e| e as sim_core::Failure)?;
                if options.motor_count != count || reset_timeout.is_zero() {
                    return Err(admission(
                        "ROS2 motor count must match worker and reset timeout be positive",
                    ));
                }
            }
        }
        Ok(())
    }
    pub fn create(&self, count: usize, dt: f64, limit: f64) -> Result<Box<dyn WorkerEngine>> {
        self.validate(count, dt, limit)?;
        match self {
            Self::Null => Ok(Box::new(NullEngine::new(count, limit)?)),
            #[cfg(feature = "mujoco")]
            Self::Mujoco { model } => Ok(Box::new(MujocoEngine::new(model, count, dt, limit)?)),
            #[cfg(feature = "ros2")]
            Self::Ros2 {
                options,
                wait_for_imu,
                reset_timeout,
            } => Ok(Box::new(
                crate::ros2::Ros2Engine::new(options, limit, *wait_for_imu, *reset_timeout)
                    .map_err(|e| e as sim_core::Failure)?,
            )),
        }
    }
}

pub struct NullEngine {
    rotors: Vec<Rotor>,
    policies: Vec<MotorPolicy>,
}
impl NullEngine {
    pub fn new(count: usize, limit: f64) -> Result<Self> {
        if count > 1024 {
            return Err(admission("too many worker motors"));
        }
        Ok(Self {
            rotors: (0..count)
                .map(|_| Rotor::new(RotorOptions::default()))
                .collect::<Result<_>>()?,
            policies: vec![MotorPolicy::new(limit)?; count],
        })
    }
}
impl WorkerEngine for NullEngine {
    fn reset(&mut self) -> Result<()> {
        for r in &mut self.rotors {
            r.reset();
        }
        for p in &mut self.policies {
            p.reset();
        }
        Ok(())
    }
    fn command(&mut self, c: MotorCommand) -> Result<()> {
        if let Some(p) = self.policies.get_mut(c.index) {
            c.apply(p)?;
            if c.action == MotorAction::Reset {
                self.rotors[c.index].reset();
            }
        }
        Ok(())
    }
    fn step(&mut self, dt: f64) -> Result<EngineSample> {
        if !dt.is_finite() || dt <= 0.0 {
            return Err(admission("worker native dt must be finite and positive"));
        }
        for (r, p) in self.rotors.iter_mut().zip(&self.policies) {
            let (q, v) = r.state();
            r.step(dt, p.effort(q, v)?)?;
        }
        Ok(EngineSample {
            imu: ImuSample {
                gyro_rad_s: [0.0; 3],
                accel_m_s2: [0.0, 0.0, 9.80665],
            },
            source_time_ns: None,
            motors: self
                .rotors
                .iter()
                .map(|r| {
                    let (q, v) = r.state();
                    (q, v, r.effort())
                })
                .collect(),
        })
    }
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
}

#[cfg(feature = "mujoco")]
pub struct MujocoEngine {
    physics: crate::physics::mujoco::MujocoPhysics,
    names: Vec<String>,
    policies: Vec<MotorPolicy>,
    efforts: Vec<f64>,
}
#[cfg(feature = "mujoco")]
impl MujocoEngine {
    pub fn new(path: &std::path::Path, count: usize, dt: f64, limit: f64) -> Result<Self> {
        use crate::physics::mujoco::{CompiledModel, MujocoBindings, MujocoPhysics};
        use mujoco_rs::wrappers::mj_model::MjtObj;
        if !dt.is_finite() || dt <= 0.0 || count > 1024 {
            return Err(admission("invalid legacy MuJoCo count or timestep"));
        }
        let policy = MotorPolicy::new(limit)?;
        let mut model = CompiledModel::from_xml(
            path.to_str()
                .ok_or_else(|| admission("MuJoCo path must be UTF-8"))?,
        )
        .map_err(|e| admission(&e.to_string()))?;
        model.opt_mut().timestep = dt;
        let count = count.min(model.nu() as usize);
        let mut joints = Vec::new();
        for a in 0..count {
            let j = usize::try_from(model.actuator_trnid()[a][0])
                .map_err(|_| admission("legacy actuator needs a named joint"))?;
            let joint = model
                .id_to_name(MjtObj::mjOBJ_JOINT, j)
                .ok_or_else(|| admission("legacy actuator joint must be named"))?;
            let actuator = model
                .id_to_name(MjtObj::mjOBJ_ACTUATOR, a)
                .ok_or_else(|| admission("legacy actuator must be named"))?;
            joints.push((joint.to_string(), actuator.to_string()));
        }
        let sensor = |candidates: &[&str]| -> Result<String> {
            candidates
                .iter()
                .find(|s| model.name_to_id(MjtObj::mjOBJ_SENSOR, s).is_some())
                .map(|s| s.to_string())
                .ok_or_else(|| {
                    admission("legacy MuJoCo model needs named native gyro and accelerometer")
                })
        };
        let bindings = MujocoBindings {
            joints,
            imus: vec![(
                "imu".into(),
                sensor(&["imu_gyro", "gyro"])?,
                sensor(&["imu_accel", "accelerometer", "accel"])?,
            )],
        };
        let names = bindings.joints.iter().map(|(j, _)| j.clone()).collect();
        Ok(Self {
            physics: MujocoPhysics::from_model(model, bindings)?,
            names,
            policies: vec![policy; count],
            efforts: vec![0.0; count],
        })
    }
    pub fn time_s(&self) -> Result<f64> {
        self.physics.time_s()
    }
    /// Read the completed native joint after a command gate changes, without
    /// performing another integration step.
    pub fn motor_feedback(&self, index: usize) -> Result<(f64, f64, f64)> {
        let name = self
            .names
            .get(index)
            .ok_or_else(|| admission("legacy motor feedback index out of range"))?;
        let state = self.physics.get_joint_state(name)?;
        Ok((
            state.position_rad,
            state.velocity_rad_s,
            self.efforts[index],
        ))
    }
}
#[cfg(feature = "mujoco")]
impl WorkerEngine for MujocoEngine {
    fn reset(&mut self) -> Result<()> {
        self.physics.reset()?;
        for p in &mut self.policies {
            p.reset();
        }
        self.efforts.fill(0.0);
        Ok(())
    }
    fn command(&mut self, c: MotorCommand) -> Result<()> {
        let Some(p) = self.policies.get_mut(c.index) else {
            return Ok(());
        };
        c.apply(p)?;
        // Reset disables the motor without teleporting a shared native joint.
        if p.command().is_none() || !p.enabled() {
            let s = self.physics.get_joint_state(&self.names[c.index])?;
            let e = p.effort(s.position_rad, s.velocity_rad_s)?;
            self.efforts[c.index] = e;
            self.physics.set_joint_effort(&self.names[c.index], e)?;
        }
        Ok(())
    }
    fn step(&mut self, dt: f64) -> Result<EngineSample> {
        for i in 0..self.names.len() {
            let s = self.physics.get_joint_state(&self.names[i])?;
            self.efforts[i] = self.policies[i].effort(s.position_rad, s.velocity_rad_s)?;
            self.physics
                .set_joint_effort(&self.names[i], self.efforts[i])?;
        }
        self.physics.step_seconds(dt)?;
        let motors = self
            .names
            .iter()
            .zip(&self.efforts)
            .map(|(n, e)| {
                let s = self.physics.get_joint_state(n)?;
                Ok((s.position_rad, s.velocity_rad_s, *e))
            })
            .collect::<Result<_>>()?;
        Ok(EngineSample {
            imu: self.physics.get_imu("imu")?,
            source_time_ns: None,
            motors,
        })
    }
    fn close(&mut self) -> Result<()> {
        self.physics.close()
    }
}

#[cfg(feature = "ros2")]
impl WorkerEngine for crate::ros2::Ros2Engine {
    fn reset(&mut self) -> Result<()> {
        self.reset().map_err(|e| e as sim_core::Failure)
    }
    fn command(&mut self, c: MotorCommand) -> Result<()> {
        match c.action {
            MotorAction::Legacy(v) => self.set_motor(c.index, v),
            MotorAction::Reset => self.reset_motor(c.index),
            MotorAction::Enable(v) => self.set_motor_enabled(c.index, v),
            MotorAction::Mit(v) => self.set_motor_dm(c.index, v),
        }
        .map_err(|e| e as sim_core::Failure)
    }
    fn step(&mut self, dt: f64) -> Result<EngineSample> {
        let dt = std::time::Duration::try_from_secs_f64(dt)
            .map_err(|_| admission("ROS2 dt must fit Duration"))?;
        let s = self.step(dt).map_err(|e| e as sim_core::Failure)?;
        let motors = (0..s.position_rad.len())
            .map(|i| self.motor_feedback(i).map_err(|e| e as sim_core::Failure))
            .collect::<Result<_>>()?;
        Ok(EngineSample {
            imu: s.imu,
            source_time_ns: s.source_time_ns,
            motors,
        })
    }
    fn close(&mut self) -> Result<()> {
        self.close().map_err(|e| e as sim_core::Failure)
    }
}
