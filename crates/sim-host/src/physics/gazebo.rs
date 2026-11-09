//! Named SI observations through the existing Gazebo8.15 NativeWorld owner.
//! CXX owns only values, exception conversion and the native object's lifetime.

mod ffi;
use super::{BodyPose, CameraFrame, ImuSample, JointPhysics, JointState, LidarScan, NativePhysics};
use ffi::bridge as native;
use sim_core::{Error, Failure, Result};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
    sync::Arc,
};

fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn failure(error: cxx::Exception) -> Failure {
    match error.what().strip_prefix("admission: ") {
        Some(message) => admission(message),
        None => Arc::new(Error::State(format!("Gazebo: {error}"))),
    }
}
fn name(value: &str) -> Result<()> {
    if value.is_empty() || value.contains('\0') || value.contains("::") {
        return Err(admission(
            "Gazebo names must be nonempty and unscoped without NUL",
        ));
    }
    Ok(())
}

#[derive(Clone, Debug, Default)]
pub struct GazeboBindings {
    /// (logical alias, native model, native joint).
    pub joints: Vec<(String, String, String)>,
    /// (logical alias, native model, native link).
    pub bodies: Vec<(String, String, String)>,
    /// (logical alias, native model, native link, native sensor).
    pub imus: Vec<(String, String, String, String)>,
    /// (logical alias, native model, native link, native sensor).
    pub cameras: Vec<(String, String, String, String)>,
    /// (logical alias, native model, native link, native sensor).
    pub lidars: Vec<(String, String, String, String)>,
}
impl GazeboBindings {
    /// Pure shape/name admission before native SDF parsing or server creation.
    pub fn validate(&self) -> Result<()> {
        if self.joints.is_empty()
            && self.bodies.is_empty()
            && self.imus.is_empty()
            && self.cameras.is_empty()
            && self.lidars.is_empty()
        {
            return Err(admission(
                "Gazebo requires at least one observation binding",
            ));
        }
        if !cfg!(feature = "gazebo-rendering")
            && (!self.cameras.is_empty() || !self.lidars.is_empty())
        {
            return Err(admission(
                "camera/lidar requires the gazebo-rendering build feature",
            ));
        }
        for bindings in [&self.joints, &self.bodies] {
            let (mut aliases, mut targets) = (BTreeSet::new(), BTreeSet::new());
            for (alias, model, element) in bindings {
                for value in [alias, model, element] {
                    name(value)?;
                }
                if !aliases.insert(alias) || !targets.insert((model, element)) {
                    return Err(admission("Gazebo aliases and native targets must be distinct within each binding kind"));
                }
            }
        }
        for bindings in [&self.imus, &self.cameras, &self.lidars] {
            let (mut aliases, mut targets) = (BTreeSet::new(), BTreeSet::new());
            for (alias, model, link, sensor) in bindings {
                for value in [alias, model, link, sensor] {
                    name(value)?;
                }
                if !aliases.insert(alias) || !targets.insert((model, link, sensor)) {
                    return Err(admission(
                        "Gazebo sensor aliases and native targets must be distinct within each binding kind",
                    ));
                }
            }
        }
        Ok(())
    }
}

pub(super) fn resource_directories(resource_paths: &[PathBuf]) -> Result<Vec<String>> {
    resource_paths
        .iter()
        .map(|path| {
            let text = path
                .to_str()
                .ok_or_else(|| admission("Gazebo resource paths must be UTF-8"))?;
            if text.is_empty() || text.contains(['\0', ':']) {
                return Err(admission(
                    "Gazebo resource paths require directories without NUL/colon",
                ));
            }
            let path = path
                .canonicalize()
                .map_err(|error| admission(format!("Gazebo resource path: {error}")))?;
            if !path.is_dir() {
                return Err(admission("Gazebo resource path must be a directory"));
            }
            let text = path
                .to_str()
                .ok_or_else(|| admission("Gazebo resource paths must be UTF-8"))?;
            if text.contains(':') {
                return Err(admission("Gazebo resource paths must not contain colon"));
            }
            Ok(text.to_owned())
        })
        .collect::<Result<Vec<_>>>()
}

pub struct GazeboPhysics {
    world: cxx::UniquePtr<native::World>,
}
impl GazeboPhysics {
    pub fn from_sdf_path(
        path: impl AsRef<Path>,
        bindings: GazeboBindings,
        resource_paths: &[PathBuf],
    ) -> Result<Self> {
        bindings.validate()?;
        let path = path
            .as_ref()
            .to_str()
            .ok_or_else(|| admission("Gazebo SDF path must be UTF-8"))?;
        if path.is_empty() || path.contains('\0') {
            return Err(admission("Gazebo SDF path must be nonempty without NUL"));
        }
        let resources = resource_directories(resource_paths)?;
        if native::version() != "8.15.0" {
            return Err(Arc::new(Error::BackendUnavailable(
                "Gazebo adapter requires native8.15.0".into(),
            )));
        }
        let pairs = |values: Vec<(String, String, String)>| {
            values
                .into_iter()
                .map(|(alias, model, element)| native::Binding {
                    alias,
                    model,
                    element,
                })
                .collect::<Vec<_>>()
        };
        let joints = pairs(bindings.joints);
        let bodies = pairs(bindings.bodies);
        let sensors = |values: Vec<(String, String, String, String)>| {
            values
                .into_iter()
                .map(|(alias, model, link, sensor)| native::SensorBinding {
                    alias,
                    model,
                    link,
                    sensor,
                })
                .collect::<Vec<_>>()
        };
        let imus = sensors(bindings.imus);
        let cameras = sensors(bindings.cameras);
        let lidars = sensors(bindings.lidars);
        let world = native::new_world(path, &joints, &bodies, &imus, &resources, &cameras, &lidars)
            .map_err(failure)?;
        Ok(Self { world })
    }
    pub fn time_ns(&self) -> Result<u64> {
        self.world
            .time_ns()
            .map(|value| value as u64)
            .map_err(failure)
    }
    pub fn timestep_ns(&self) -> Result<u64> {
        self.world
            .timestep_ns()
            .map(|value| value as u64)
            .map_err(failure)
    }
    pub fn time_s(&self) -> Result<f64> {
        Ok(self.time_ns()? as f64 / 1e9)
    }
    pub fn get_body_pose(&self, name: &str) -> Result<BodyPose> {
        let value = self.world.get_body_pose(name).map_err(failure)?;
        Ok(BodyPose {
            position_m: value.position_m,
            quaternion_wxyz: value.quaternion_wxyz,
        })
    }
    pub fn get_imu(&self, name: &str) -> Result<ImuSample> {
        let value = self.world.get_imu(name).map_err(failure)?;
        Ok(ImuSample {
            gyro_rad_s: value.gyro_rad_s,
            accel_m_s2: value.accel_m_s2,
        })
    }
    /// Latest actual sensor frame, without waiting or advancing physics.
    pub fn get_camera(&self, name: &str) -> Result<Option<CameraFrame>> {
        let value = self.world.get_camera(name).map_err(failure)?;
        Ok(value.available.then_some(CameraFrame {
            timestamp_ns: value.timestamp_ns as u64,
            width: value.width,
            height: value.height,
            step: value.step,
            frame_id: value.frame_id,
            data: value.data,
        }))
    }
    /// Latest actual scan; native infinity misses and NaN vertical step survive.
    pub fn get_lidar(&self, name: &str) -> Result<Option<LidarScan>> {
        let value = self.world.get_lidar(name).map_err(failure)?;
        Ok(value.available.then_some(LidarScan {
            timestamp_ns: value.timestamp_ns as u64,
            frame_id: value.frame_id,
            angle_min_rad: value.angle_min_rad,
            angle_max_rad: value.angle_max_rad,
            angle_step_rad: value.angle_step_rad,
            range_min_m: value.range_min_m,
            range_max_m: value.range_max_m,
            vertical_angle_min_rad: value.vertical_angle_min_rad,
            vertical_angle_max_rad: value.vertical_angle_max_rad,
            vertical_angle_step_rad: value.vertical_angle_step_rad,
            ranges_m: value.ranges_m,
            intensities: value.intensities,
        }))
    }
}
fn interval(dt_ns: u64) -> Result<i64> {
    i64::try_from(dt_ns)
        .ok()
        .filter(|dt| *dt > 0)
        .ok_or_else(|| admission("Gazebo dt_ns must be a positive signed64 interval"))
}
impl JointPhysics for GazeboPhysics {
    fn reset(&mut self) -> Result<()> {
        self.world.pin_mut().reset().map_err(failure)
    }
    fn validate_step(&self, dt_ns: u64) -> Result<()> {
        self.world.validate_step(interval(dt_ns)?).map_err(failure)
    }
    fn validate_steps(&self, intervals_ns: &[u64]) -> Result<()> {
        let intervals = intervals_ns
            .iter()
            .map(|dt| interval(*dt))
            .collect::<Result<Vec<_>>>()?;
        self.world.validate_steps(&intervals).map_err(failure)
    }
    fn step(&mut self, dt_ns: u64) -> Result<()> {
        self.world.pin_mut().step(interval(dt_ns)?).map_err(failure)
    }
    fn validate_joint_effort(&self, name: &str, effort_nm: f64) -> Result<()> {
        self.world
            .validate_joint_effort(name, effort_nm)
            .map_err(failure)
    }
    fn set_joint_effort(&mut self, name: &str, effort_nm: f64) -> Result<()> {
        self.world
            .pin_mut()
            .set_joint_effort(name, effort_nm)
            .map_err(failure)
    }
    fn get_joint_state(&self, name: &str) -> Result<JointState> {
        let value = self.world.get_joint_state(name).map_err(failure)?;
        Ok(JointState {
            position_rad: value.position_rad,
            velocity_rad_s: value.velocity_rad_s,
        })
    }
    fn close(&mut self) -> Result<()> {
        self.world.pin_mut().close().map_err(failure)
    }
}
impl NativePhysics for GazeboPhysics {
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
