#[cxx::bridge(namespace = "dm_ros2")]
pub(super) mod bridge {
    struct Snapshot {
        gyro_rad_s: [f64; 3],
        accel_m_s2: [f64; 3],
        source_time_ns: u64,
        source_time_valid: bool,
        position_rad: Vec<f64>,
        velocity_rad_s: Vec<f64>,
        effort_nm: Vec<f64>,
        effort_valid: Vec<u8>,
    }

    unsafe extern "C++" {
        include!("ros_transport.hh");
        type Transport;
        fn new_transport(
            motor_count: usize,
            motor_topic: &str,
            imu_topic: &str,
            joint_state_topic: &str,
            node_name: &str,
            reset_service: &str,
        ) -> Result<UniquePtr<Transport>>;
        fn sample(self: &Transport) -> Result<Snapshot>;
        fn publish_efforts(self: Pin<&mut Transport>, values: &[f64]) -> Result<()>;
        fn pump(self: Pin<&mut Transport>, timeout_ns: u64) -> Result<()>;
        fn reset(self: Pin<&mut Transport>, timeout_ns: u64) -> Result<()>;
        fn close(self: Pin<&mut Transport>) -> Result<()>;
    }
}
