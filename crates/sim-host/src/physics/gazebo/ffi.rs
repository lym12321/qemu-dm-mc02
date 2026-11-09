#[cxx::bridge(namespace = "dm_gazebo::rust_bridge")]
pub(super) mod bridge {
    struct Binding {
        alias: String,
        model: String,
        element: String,
    }
    struct SensorBinding {
        alias: String,
        model: String,
        link: String,
        sensor: String,
    }
    struct JointSample {
        position_rad: f64,
        velocity_rad_s: f64,
    }
    struct BodySample {
        position_m: [f64; 3],
        quaternion_wxyz: [f64; 4],
    }
    struct ImuSample {
        gyro_rad_s: [f64; 3],
        accel_m_s2: [f64; 3],
    }
    struct CameraSample {
        available: bool,
        timestamp_ns: i64,
        width: u32,
        height: u32,
        step: u32,
        frame_id: String,
        data: Vec<u8>,
    }
    struct LidarSample {
        available: bool,
        timestamp_ns: i64,
        frame_id: String,
        angle_min_rad: f64,
        angle_max_rad: f64,
        angle_step_rad: f64,
        range_min_m: f64,
        range_max_m: f64,
        vertical_angle_min_rad: f64,
        vertical_angle_max_rad: f64,
        vertical_angle_step_rad: f64,
        ranges_m: Vec<f64>,
        intensities: Vec<f64>,
    }
    unsafe extern "C++" {
        include!("world_bridge.hh");
        type World;
        fn new_world(
            path: &str,
            joints: &[Binding],
            bodies: &[Binding],
            imus: &[SensorBinding],
            resource_paths: &[String],
            cameras: &[SensorBinding],
            lidars: &[SensorBinding],
        ) -> Result<UniquePtr<World>>;
        fn version() -> String;
        fn time_ns(self: &World) -> Result<i64>;
        fn timestep_ns(self: &World) -> Result<i64>;
        fn validate_step(self: &World, dt_ns: i64) -> Result<()>;
        fn validate_steps(self: &World, intervals_ns: &[i64]) -> Result<()>;
        fn validate_joint_effort(self: &World, name: &str, effort_nm: f64) -> Result<()>;
        fn get_joint_state(self: &World, name: &str) -> Result<JointSample>;
        fn get_body_pose(self: &World, name: &str) -> Result<BodySample>;
        fn get_imu(self: &World, name: &str) -> Result<ImuSample>;
        fn get_camera(self: &World, name: &str) -> Result<CameraSample>;
        fn get_lidar(self: &World, name: &str) -> Result<LidarSample>;
        fn set_joint_effort(self: Pin<&mut World>, name: &str, effort_nm: f64) -> Result<()>;
        fn step(self: Pin<&mut World>, dt_ns: i64) -> Result<()>;
        fn reset(self: Pin<&mut World>) -> Result<()>;
        fn close(self: Pin<&mut World>) -> Result<()>;
    }
}
