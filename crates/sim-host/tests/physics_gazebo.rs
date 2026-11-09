#![cfg(feature = "gazebo")]

use sim_core::Error;
use sim_host::physics::{
    gazebo::{GazeboBindings, GazeboPhysics},
    JointPhysics, JointState,
};
use std::{path::PathBuf, sync::Mutex};

static NATIVE: Mutex<()> = Mutex::new(());
const ROTOR: &str = include_str!("../../../models/gazebo_rotor.sdf");
fn bindings(imu: bool) -> GazeboBindings {
    GazeboBindings {
        joints: vec![("shaft".into(), "rotor".into(), "shaft".into())],
        bodies: vec![("body".into(), "rotor".into(), "body".into())],
        imus: if imu {
            vec![("imu".into(), "rotor".into(), "body".into(), "imu".into())]
        } else {
            vec![]
        },
        ..Default::default()
    }
}
fn file(xml: &str) -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("world.sdf");
    std::fs::write(&path, xml).unwrap();
    (directory, path)
}
fn admission(error: sim_core::Failure) {
    assert!(
        matches!(error.downcast_ref::<Error>(), Some(Error::Admission(_))),
        "{error}"
    );
}
fn near(actual: f64, expected: f64, tolerance: f64) {
    assert!(
        (actual - expected).abs() <= tolerance,
        "{actual} != {expected} within {tolerance}"
    );
}
fn imu_sdf(pose: &str) -> String {
    ROTOR.replace("<gravity>0 0 0</gravity>", "<gravity>0 0 -10</gravity>").replace("</inertial>", &format!("</inertial><sensor name=\"imu\" type=\"imu\"><pose>{pose}</pose><update_rate>0</update_rate><imu/></sensor>"))
}

#[test]
fn public_names_duplicates_and_resource_paths_reject_before_sdf_acquisition() {
    assert!(GazeboBindings::default().validate().is_err());
    for (alias, model) in [("", "rotor"), ("a\0b", "rotor"), ("shaft", "nested::model")] {
        let mut values = bindings(false);
        values.joints[0].0 = alias.into();
        values.joints[0].1 = model.into();
        let error = match GazeboPhysics::from_sdf_path("nonexistent.sdf", values, &[]) {
            Err(error) => error,
            Ok(_) => panic!("invalid native name"),
        };
        assert!(error.to_string().contains("unscoped"));
        admission(error);
    }
    let mut values = bindings(false);
    values
        .joints
        .push(("other".into(), "rotor".into(), "shaft".into()));
    assert!(values
        .validate()
        .unwrap_err()
        .to_string()
        .contains("distinct"));
    values = bindings(false);
    values
        .joints
        .push(("shaft".into(), "rotor".into(), "other".into()));
    assert!(values
        .validate()
        .unwrap_err()
        .to_string()
        .contains("distinct"));
    for path in [
        PathBuf::from(""),
        PathBuf::from("missing-directory"),
        PathBuf::from("a:b"),
    ] {
        let error = match GazeboPhysics::from_sdf_path("nonexistent.sdf", bindings(false), &[path])
        {
            Err(error) => error,
            Ok(_) => panic!("invalid resource path"),
        };
        admission(error);
    }
}

#[test]
fn native_drive_coast_body_reset_and_idempotent_close_use_completed_state() {
    let _guard = NATIVE.lock().unwrap();
    let (_directory, path) = file(ROTOR);
    let mut physics = GazeboPhysics::from_sdf_path(&path, bindings(false), &[]).unwrap();
    assert_eq!(
        (physics.time_ns().unwrap(), physics.timestep_ns().unwrap()),
        (0, 1_000_000)
    );
    physics.set_joint_effort("shaft", 0.4).unwrap();
    for dt in [0, 1, 10_001_000_000, u64::MAX] {
        admission(physics.validate_step(dt).unwrap_err());
        admission(physics.step(dt).unwrap_err());
    }
    for effort in [f64::NAN, f64::INFINITY, 10.01] {
        admission(physics.validate_joint_effort("shaft", effort).unwrap_err());
        admission(physics.set_joint_effort("shaft", effort).unwrap_err());
    }
    admission(physics.get_joint_state("missing").unwrap_err());
    physics.validate_steps(&[3_000_000, 7_000_000]).unwrap();
    assert_eq!(physics.time_ns().unwrap(), 0);
    physics.step(100_000_000).unwrap();
    let state = physics.get_joint_state("shaft").unwrap();
    near(state.velocity_rad_s, 2.0, 1e-9);
    near(state.position_rad, 0.1, 0.0021);
    let pose = physics.get_body_pose("body").unwrap();
    assert_eq!(pose.position_m, [0.0; 3]);
    near(
        2.0 * pose.quaternion_wxyz[3].atan2(pose.quaternion_wxyz[0]),
        state.position_rad,
        1e-12,
    );
    assert_eq!(physics.get_joint_state("shaft").unwrap(), state);
    assert_eq!(physics.time_s().unwrap(), 0.1);
    #[cfg(target_os = "linux")]
    assert!(
        !std::fs::read_to_string("/proc/self/maps")
            .unwrap()
            .contains("libpython"),
        "the native Gazebo SDK must be built with SKIP_PYBIND11=ON"
    );
    physics.set_joint_effort("shaft", 0.0).unwrap();
    physics.step(10_000_000).unwrap();
    near(
        physics.get_joint_state("shaft").unwrap().position_rad - state.position_rad,
        0.02,
        1e-9,
    );
    std::fs::remove_file(path).unwrap();
    physics.reset().unwrap();
    physics.step(1_000_000).unwrap();
    assert_eq!(
        physics.get_joint_state("shaft").unwrap(),
        JointState {
            position_rad: 0.0,
            velocity_rad_s: 0.0
        }
    );
    physics.close().unwrap();
    physics.close().unwrap();
    let error = physics.reset().unwrap_err();
    assert!(matches!(
        error.downcast_ref::<Error>(),
        Some(Error::State(_))
    ));
    assert!(physics
        .get_body_pose("body")
        .unwrap_err()
        .to_string()
        .contains("closed"));
}

#[test]
fn native_tick_cast_and_interval_preflight_preserve_integer_nanoseconds() {
    let _guard = NATIVE.lock().unwrap();
    for (seconds, tick) in [("0.000001001", 1001), ("0.001001", 1_000_999)] {
        let (_directory, path) = file(&ROTOR.replace(
            "<max_step_size>0.001</max_step_size>",
            &format!("<max_step_size>{seconds}</max_step_size>"),
        ));
        let mut physics = GazeboPhysics::from_sdf_path(path, bindings(false), &[]).unwrap();
        assert_eq!(physics.timestep_ns().unwrap(), tick);
        for intervals in [vec![], vec![tick, tick + 1]] {
            admission(physics.validate_steps(&intervals).unwrap_err());
        }
        physics.validate_step(10_000 * tick).unwrap();
        admission(physics.validate_step(10_001 * tick).unwrap_err());
        physics.validate_steps(&[tick, 2 * tick]).unwrap();
        assert_eq!(physics.time_ns().unwrap(), 0);
        physics.step(3 * tick).unwrap();
        assert_eq!(physics.time_ns().unwrap(), 3 * tick);
        physics.close().unwrap();
    }
}

#[test]
fn native_imu_observes_rotated_offset_target_and_freefall_without_a_rust_sensor_model() {
    let _guard = NATIVE.lock().unwrap();
    let xml = imu_sdf(".25 0 0 1.5707963267948966 0 0");
    let (_directory, path) = file(&xml);
    let mut physics = GazeboPhysics::from_sdf_path(&path, bindings(true), &[]).unwrap();
    physics.set_joint_effort("shaft", 0.4).unwrap();
    physics.step(100_000_000).unwrap();
    let sample = physics.get_imu("imu").unwrap();
    for (actual, expected) in sample
        .gyro_rad_s
        .into_iter()
        .chain(sample.accel_m_s2)
        .zip([0.0, 2.0, 0.0, -1.0, 10.0, -5.0])
    {
        near(actual, expected, 0.01);
    }
    assert_eq!(physics.get_imu("imu").unwrap(), sample);
    std::fs::remove_file(path).unwrap();
    physics.reset().unwrap();
    for (actual, expected) in physics
        .get_imu("imu")
        .unwrap()
        .accel_m_s2
        .into_iter()
        .zip([0.0, 10.0, 0.0])
    {
        near(actual, expected, 1e-12);
    }
    physics.close().unwrap();
    let start = xml.find("<joint name=\"shaft\"").unwrap();
    let end = xml[start..].find("</joint>").unwrap() + start + 8;
    let freefall = format!("{}{}", &xml[..start], &xml[end..]);
    let (_directory, path) = file(&freefall);
    let mut values = bindings(true);
    values.joints.clear();
    let mut physics = GazeboPhysics::from_sdf_path(path, values, &[]).unwrap();
    physics.step(10_000_000).unwrap();
    for value in physics.get_imu("imu").unwrap().accel_m_s2 {
        near(value, 0.0, 1e-12);
    }
    physics.close().unwrap();
}

#[test]
fn native_binding_rejection_and_failed_engine_initialization_release_the_server() {
    let _guard = NATIVE.lock().unwrap();
    let xml=imu_sdf("0 0 0 0 0 0").replace("name=\"gz::sim::systems::Physics\"/>","name=\"gz::sim::systems::Physics\"><engine><filename>dm-test-missing-physics-engine</filename></engine></plugin>");
    let (_directory, path) = file(&xml);
    let mut bad = bindings(true);
    bad.imus[0].3 = "missing".into();
    let error = match GazeboPhysics::from_sdf_path(&path, bad, &[]) {
        Err(error) => error,
        Ok(_) => panic!("missing sensor"),
    };
    admission(error);
    let error = match GazeboPhysics::from_sdf_path(&path, bindings(true), &[]) {
        Err(error) => error,
        Ok(_) => panic!("missing engine"),
    };
    assert!(
        matches!(error.downcast_ref::<Error>(), Some(Error::State(_))),
        "{error}"
    );
    assert!(error.to_string().contains("unavailable"));
    std::fs::write(&path, imu_sdf("0 0 0 0 0 0")).unwrap();
    let mut physics = GazeboPhysics::from_sdf_path(path, bindings(true), &[]).unwrap();
    physics.step(1_000_000).unwrap();
    assert_eq!(physics.get_imu("imu").unwrap().accel_m_s2, [0.0, 0.0, 10.0]);
    physics.close().unwrap();
}
