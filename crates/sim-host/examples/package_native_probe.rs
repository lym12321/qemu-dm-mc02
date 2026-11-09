//! Distribution acceptance only; this executable is not a product entry point.
#[cfg(feature = "linux-runtime")]
fn main() {
    use sim_host::{
        physics::{
            gazebo::{GazeboBindings, GazeboPhysics},
            mujoco::{MujocoBindings, MujocoPhysics},
            JointPhysics,
        },
        ros2::{Ros2Options, Ros2Transport},
    };
    use std::time::{Duration, Instant};
    sim_host::bundle::configure().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let sdf = dir.path().join("rendering.sdf");
    std::fs::write(
        &sdf,
        include_str!("../../../sim/physics/gazebo_native/tests/rendering.sdf"),
    )
    .unwrap();
    let mut gazebo = GazeboPhysics::from_sdf_path(
        sdf,
        GazeboBindings {
            cameras: vec![(
                "front".into(),
                "sensors".into(),
                "body".into(),
                "camera".into(),
            )],
            lidars: vec![(
                "scan".into(),
                "sensors".into(),
                "body".into(),
                "lidar".into(),
            )],
            ..Default::default()
        },
        &[],
    )
    .unwrap();
    let until = Instant::now() + Duration::from_secs(8);
    let (mut image, mut scan) = (None, None);
    while image.is_none() || scan.is_none() {
        assert!(Instant::now() < until, "packaged rendering did not publish");
        gazebo.step(1_000_000).unwrap();
        image = gazebo.get_camera("front").unwrap();
        scan = gazebo.get_lidar("scan").unwrap();
        std::thread::sleep(Duration::from_millis(1));
    }
    let (image, scan) = (image.unwrap(), scan.unwrap());
    assert_eq!((image.width, image.height), (64, 48));
    assert_eq!(scan.ranges_m.len(), 65);
    assert!((scan.ranges_m[32] - 2.5).abs() < 0.02);
    gazebo.close().unwrap();
    let mut mujoco = MujocoPhysics::from_xml_string(
        r#"<mujoco><option timestep="0.001" gravity="0 0 0"/>
        <worldbody><body><joint name="shaft" type="hinge" axis="0 0 1"/>
        <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/>
        </body></worldbody><actuator><motor name="drive" joint="shaft"/></actuator></mujoco>"#,
        MujocoBindings {
            joints: vec![("shaft".into(), "drive".into())],
            imus: vec![],
        },
    )
    .unwrap();
    mujoco.set_joint_effort("shaft", 1.0).unwrap();
    mujoco.step(1_000_000).unwrap();
    assert!((mujoco.get_joint_state("shaft").unwrap().velocity_rad_s - 0.05).abs() < 1e-12);
    mujoco.close().unwrap();
    let mut ros = Ros2Transport::new(&Ros2Options {
        motor_count: 0,
        motor_topic: "/package/motor".into(),
        imu_topic: "/package/imu".into(),
        joint_state_topic: "/package/joints".into(),
        node_name: "package_native_probe".into(),
        reset_service: String::new(),
    })
    .unwrap();
    ros.sample().unwrap();
    ros.close().unwrap();
    let maps = std::fs::read_to_string("/proc/self/maps").unwrap();
    assert!(!maps.contains("libpython"));
    assert!(!maps.contains("/opt/ros/") && !maps.contains("/opt/mujoco"));
    assert!(!maps.contains("/build/rust-gazebo-native/"));
    println!("native package: actual MuJoCo rotor/Ogre2 image/lidar/ROS context PASS; no Python/SDK library maps");
}

#[cfg(not(feature = "linux-runtime"))]
fn main() {
    panic!("package native acceptance requires --features linux-runtime")
}
