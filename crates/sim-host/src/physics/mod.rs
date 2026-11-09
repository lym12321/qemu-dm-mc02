//! Named native plant observations in SI units. Execution and native time stay
//! with the physics owner; these interfaces contain no board or wire policy.

pub mod binding;
#[cfg(feature = "simulation")]
pub mod factory;
#[cfg(feature = "gazebo")]
pub mod gazebo;
pub mod joints;
#[cfg(feature = "mujoco")]
pub mod mujoco;
pub mod participant;
pub mod rotor;

use sim_core::Result;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct JointState {
    pub position_rad: f64,
    pub velocity_rad_s: f64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BodyPose {
    pub position_m: [f64; 3],
    pub quaternion_wxyz: [f64; 4],
}

pub use crate::signals::ImuSample;

/// Owned latest native RGB8 image. Its timestamp belongs to the sensor producer.
#[derive(Clone, Debug, PartialEq)]
pub struct CameraFrame {
    pub timestamp_ns: u64,
    pub width: u32,
    pub height: u32,
    pub step: u32,
    pub frame_id: String,
    pub data: Vec<u8>,
}

/// Native single-row scan, preserving infinity misses and NaN vertical step.
#[derive(Clone, Debug, PartialEq)]
pub struct LidarScan {
    pub timestamp_ns: u64,
    pub frame_id: String,
    pub angle_min_rad: f64,
    pub angle_max_rad: f64,
    pub angle_step_rad: f64,
    pub range_min_m: f64,
    pub range_max_m: f64,
    pub vertical_angle_min_rad: f64,
    pub vertical_angle_max_rad: f64,
    pub vertical_angle_step_rad: f64,
    pub ranges_m: Vec<f64>,
    pub intensities: Vec<f64>,
}

/// Exclusive joint execution capability. Validators do not write controls or
/// advance native time, and do not reserve or guarantee a successful step.
pub trait JointPhysics {
    fn reset(&mut self) -> Result<()>;
    fn validate_step(&self, dt_ns: u64) -> Result<()>;
    fn validate_steps(&self, intervals_ns: &[u64]) -> Result<()>;
    fn step(&mut self, dt_ns: u64) -> Result<()>;
    fn validate_joint_effort(&self, name: &str, effort_nm: f64) -> Result<()>;
    fn set_joint_effort(&mut self, name: &str, effort_nm: f64) -> Result<()>;
    fn get_joint_state(&self, name: &str) -> Result<JointState>;
    fn close(&mut self) -> Result<()>;
}

/// Completed observations from one native world. Native floating time is
/// independent of the integer execution session, and getters do not advance it.
pub trait NativePhysics: JointPhysics {
    fn time_s(&self) -> Result<f64>;
    fn get_body_pose(&self, name: &str) -> Result<BodyPose>;
    fn get_imu(&self, name: &str) -> Result<ImuSample>;
}

impl<P: JointPhysics + ?Sized> JointPhysics for Box<P> {
    fn reset(&mut self) -> Result<()> {
        (**self).reset()
    }
    fn validate_step(&self, dt_ns: u64) -> Result<()> {
        (**self).validate_step(dt_ns)
    }
    fn validate_steps(&self, intervals_ns: &[u64]) -> Result<()> {
        (**self).validate_steps(intervals_ns)
    }
    fn step(&mut self, dt_ns: u64) -> Result<()> {
        (**self).step(dt_ns)
    }
    fn validate_joint_effort(&self, name: &str, effort_nm: f64) -> Result<()> {
        (**self).validate_joint_effort(name, effort_nm)
    }
    fn set_joint_effort(&mut self, name: &str, effort_nm: f64) -> Result<()> {
        (**self).set_joint_effort(name, effort_nm)
    }
    fn get_joint_state(&self, name: &str) -> Result<JointState> {
        (**self).get_joint_state(name)
    }
    fn close(&mut self) -> Result<()> {
        (**self).close()
    }
}

impl<P: NativePhysics + ?Sized> NativePhysics for Box<P> {
    fn time_s(&self) -> Result<f64> {
        (**self).time_s()
    }
    fn get_body_pose(&self, name: &str) -> Result<BodyPose> {
        (**self).get_body_pose(name)
    }
    fn get_imu(&self, name: &str) -> Result<ImuSample> {
        (**self).get_imu(name)
    }
}
