#![cfg(feature = "mujoco")]

//! Native public consumers with Newton/Euler and rigid-body SI expectations.
//! These fixtures do not attest calibrated hardware or CPU/plant lockstep.

use mujoco_rs::prelude::{MjtDisableBit, MjtObj};
use sim_host::physics::{
    mujoco::{CompiledModel, MujocoBindings, MujocoPhysics},
    ImuSample, JointPhysics, JointState,
};

const ROTOR: &str = r#"<mujoco>
  <compiler angle="radian"/>
  <option timestep="0.001" integrator="Euler" gravity="0 0 -9.81"/>
  <worldbody><body name="rotor">
    <joint name="shaft" type="hinge" axis="0 0 1" ref="0"/>
    <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/>
    <site name="site" pos="0.25 0 0"/><site name="other_site"/>
  </body></worldbody>
  <actuator><motor name="drive" joint="shaft"/></actuator>
  <sensor><gyro name="gyro" site="site"/>
    <accelerometer name="accel" site="site"/></sensor>
</mujoco>"#;

fn bindings() -> MujocoBindings {
    MujocoBindings {
        joints: vec![("shaft".into(), "drive".into())],
        imus: vec![("imu".into(), "gyro".into(), "accel".into())],
    }
}
fn rotor() -> MujocoPhysics {
    MujocoPhysics::from_xml_string(ROTOR, bindings()).unwrap()
}
fn near(actual: f64, expected: f64, tolerance: f64) {
    assert!(
        (actual - expected).abs() <= tolerance,
        "{actual:?} != {expected:?} ± {tolerance}"
    );
}
fn vector<const N: usize>(actual: [f64; N], expected: [f64; N]) {
    for (actual, expected) in actual.into_iter().zip(expected) {
        near(actual, expected, 1e-12);
    }
}

#[test]
fn public_value_and_binding_admission_reject_invalid_shapes_before_native_input() {
    let sample = ImuSample {
        gyro_rad_s: [0.0; 3],
        accel_m_s2: [0.0, 0.0, 9.81],
    };
    sample.validate().unwrap();
    for value in [f64::NAN, f64::INFINITY] {
        assert!(ImuSample {
            gyro_rad_s: [value; 3],
            ..sample
        }
        .validate()
        .is_err());
        assert!(ImuSample {
            accel_m_s2: [value; 3],
            ..sample
        }
        .validate()
        .is_err());
    }
    for invalid in [
        MujocoBindings {
            joints: vec![("".into(), "drive".into())],
            imus: vec![],
        },
        MujocoBindings {
            joints: vec![("shaft".into(), "drive\0".into())],
            imus: vec![],
        },
        MujocoBindings {
            joints: vec![("shaft".into(), "a".into()), ("shaft".into(), "b".into())],
            imus: vec![],
        },
        MujocoBindings {
            joints: vec![("a".into(), "drive".into()), ("b".into(), "drive".into())],
            imus: vec![],
        },
        MujocoBindings {
            joints: vec![],
            imus: vec![
                ("imu".into(), "g".into(), "a".into()),
                ("imu".into(), "g".into(), "a".into()),
            ],
        },
    ] {
        assert!(invalid.validate().is_err());
    }
    assert!(MujocoPhysics::from_xml_string("\0", bindings()).is_err());
    assert!(MujocoPhysics::from_xml_path("bad\0path", bindings()).is_err());
}

#[test]
fn positive_negative_effort_and_coast_match_independent_euler_solution() {
    let mut physics = rotor();
    let initial = physics.get_joint_state("shaft").unwrap();
    assert_eq!(
        initial,
        JointState {
            position_rad: 0.0,
            velocity_rad_s: 0.0
        }
    );
    // Izz=.02, tau=.4 gives alpha=20. Euler updates velocity then position.
    for (effort, steps, omega, theta) in [
        (0.4, 10, 0.2, 0.0011),
        (-0.4, 5, 0.1, 0.0018),
        (0.0, 7, 0.1, 0.0025),
    ] {
        physics.set_joint_effort("shaft", effort).unwrap();
        for _ in 0..steps {
            physics.step(1_000_000).unwrap();
        }
        let state = physics.get_joint_state("shaft").unwrap();
        near(state.velocity_rad_s, omega, 1e-13);
        near(state.position_rad, theta, 1e-13);
    }
    near(physics.time_s().unwrap(), 0.022, 1e-15);
    assert_eq!(initial.position_rad, 0.0); // Completed values are snapshots.
}

#[test]
fn pose_gyro_and_proper_acceleration_observe_the_integrated_target() {
    let mut physics = rotor();
    physics.set_joint_effort("shaft", 0.4).unwrap();
    physics.step(10_000_000).unwrap();
    let pose = physics.get_body_pose("rotor").unwrap();
    vector(pose.position_m, [0.0; 3]);
    // One Euler step gives omega=.2 and theta=.002.
    vector(
        pose.quaternion_wxyz,
        [0.001_f64.cos(), 0.0, 0.0, 0.001_f64.sin()],
    );
    let sample = physics.get_imu("imu").unwrap();
    vector(sample.gyro_rad_s, [0.0, 0.0, 0.2]);
    // alpha×r + omega×(omega×r) - gravity, expressed in site-local SI.
    vector(sample.accel_m_s2, [-0.01, 5.0, 9.81]);
    assert_eq!(physics.get_body_pose("rotor").unwrap(), pose);
    assert_eq!(physics.get_imu("imu").unwrap(), sample);
    assert_eq!(physics.time_s().unwrap(), 0.01);
}

#[test]
fn rotated_site_returns_local_gyro_and_proper_gravity() {
    let xml = ROTOR.replace(
        "pos=\"0.25 0 0\"",
        "quat=\"0.7071067811865476 0.7071067811865476 0 0\"",
    );
    let mut physics = MujocoPhysics::from_xml_string(&xml, bindings()).unwrap();
    vector(physics.get_imu("imu").unwrap().accel_m_s2, [0.0, 9.81, 0.0]);
    physics.set_joint_effort("shaft", 0.4).unwrap();
    physics.step(1_000_000).unwrap();
    vector(physics.get_imu("imu").unwrap().gyro_rad_s, [0.0, 0.02, 0.0]);
}

#[test]
fn xml_path_freefall_is_zero_proper_acceleration_and_reset_restores_pose() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("fall.xml");
    std::fs::write(
        &path,
        r#"<mujoco><option integrator="Euler" gravity="0 0 -9.81"/>
      <worldbody><body name="falling" pos="1 2 3"><freejoint/>
        <inertial pos="0 0 0" mass="1" diaginertia="0.02 0.02 0.02"/>
        <site name="site"/></body></worldbody>
      <sensor><gyro name="gyro" site="site"/>
        <accelerometer name="accel" site="site"/></sensor></mujoco>"#,
    )
    .unwrap();
    let mut only_imu = bindings();
    only_imu.joints.clear();
    let mut physics = MujocoPhysics::from_xml_path(path, only_imu).unwrap();
    vector(physics.get_imu("imu").unwrap().accel_m_s2, [0.0; 3]);
    physics.step(10_000_000).unwrap();
    vector(
        physics.get_body_pose("falling").unwrap().position_m,
        [1.0, 2.0, 2.999019],
    );
    vector(physics.get_imu("imu").unwrap().accel_m_s2, [0.0; 3]);
    physics.reset().unwrap();
    assert_eq!(physics.time_s().unwrap(), 0.0);
    assert_eq!(
        physics.get_body_pose("falling").unwrap().position_m,
        [1.0, 2.0, 3.0]
    );
}

#[test]
fn reset_restores_native_qpos0_and_clears_previous_control() {
    let mut physics =
        MujocoPhysics::from_xml_string(&ROTOR.replace("ref=\"0\"", "ref=\"0.25\""), bindings())
            .unwrap();
    let initial = physics.get_joint_state("shaft").unwrap();
    assert_eq!(
        initial,
        JointState {
            position_rad: 0.25,
            velocity_rad_s: 0.0
        }
    );
    physics.set_joint_effort("shaft", 0.4).unwrap();
    physics.step(1_000_000).unwrap();
    physics.reset().unwrap();
    assert_eq!(physics.time_s().unwrap(), 0.0);
    physics.step(1_000_000).unwrap();
    assert_eq!(physics.get_joint_state("shaft").unwrap(), initial);
}

#[test]
fn invalid_steps_efforts_and_names_preserve_time_state_and_prior_control() {
    let mut physics = rotor();
    physics.set_joint_effort("shaft", 0.4).unwrap();
    let initial = physics.get_joint_state("shaft").unwrap();
    physics.validate_step(10_000_000).unwrap();
    physics.validate_steps(&[3_000_000, 7_000_000]).unwrap();
    physics.validate_joint_effort("shaft", -0.4).unwrap();
    for dt in [0, 1_u64 << 63] {
        assert!(physics.validate_step(dt).is_err());
        assert!(physics.step(dt).is_err());
    }
    assert!(physics.validate_steps(&[]).is_err());
    for dt in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        assert!(physics.step_seconds(dt).is_err());
    }
    for effort in [f64::NAN, f64::INFINITY] {
        assert!(physics.validate_joint_effort("shaft", effort).is_err());
        assert!(physics.set_joint_effort("shaft", effort).is_err());
    }
    for missing in ["missing", "", "shaft\0"] {
        assert!(physics.set_joint_effort(missing, 0.4).is_err());
        assert!(physics.get_joint_state(missing).is_err());
        assert!(physics.get_body_pose(missing).is_err());
        assert!(physics.get_imu(missing).is_err());
    }
    assert_eq!(physics.time_s().unwrap(), 0.0);
    assert_eq!(physics.get_joint_state("shaft").unwrap(), initial);
    physics.step(1_000_000).unwrap();
    near(
        physics.get_joint_state("shaft").unwrap().velocity_rad_s,
        0.02,
        1e-13,
    );
}

#[test]
fn float_interval_is_preserved_and_unrepresentable_time_advance_is_pure_rejection() {
    let mut physics = rotor();
    let dt = 0.000001000125; // Actual fractional .125ns, no integer round trip.
    physics.step_seconds(dt).unwrap();
    assert_eq!(physics.time_s().unwrap(), dt);
    physics.reset().unwrap();
    let largest = i64::MAX as u64;
    physics.validate_step(largest).unwrap();
    assert!(physics.validate_steps(&[largest - 1, 1]).is_err());
    assert_eq!(physics.time_s().unwrap(), 0.0);
    physics.step(largest).unwrap();
    let completed = physics.time_s().unwrap();
    assert!(physics.validate_step(1).is_err());
    assert!(physics.step(1).is_err());
    assert_eq!(physics.time_s().unwrap(), completed);
    assert_eq!(
        physics.get_joint_state("shaft").unwrap(),
        JointState {
            position_rad: 0.0,
            velocity_rad_s: 0.0
        }
    );
}

#[test]
fn complete_native_bindings_reject_missing_and_unsupported_semantics() {
    for invalid in [
        MujocoBindings {
            joints: vec![("missing".into(), "drive".into())],
            imus: vec![],
        },
        MujocoBindings {
            joints: vec![("shaft".into(), "missing".into())],
            imus: vec![],
        },
        MujocoBindings {
            joints: bindings().joints,
            imus: vec![("imu".into(), "gyro".into(), "missing".into())],
        },
        MujocoBindings {
            joints: bindings().joints,
            imus: vec![("imu".into(), "gyro".into(), "gyro".into())],
        },
    ] {
        assert!(
            MujocoPhysics::from_model(CompiledModel::from_xml_string(ROTOR).unwrap(), invalid)
                .is_err()
        );
    }
    // Different model restrictions exercise different unsupported semantics,
    // rather than repeating the same type/length rejection with many values.
    for (before, after) in [
        ("type=\"hinge\"", "type=\"slide\""),
        ("<motor name=\"drive\"", "<motor name=\"drive\" gear=\"2\""),
        (
            "<motor name=\"drive\"",
            "<motor name=\"drive\" ctrlrange=\"-1 1\"",
        ),
        (
            "<motor name=\"drive\"",
            "<motor name=\"drive\" forcerange=\"-1 1\"",
        ),
        ("<gyro name=\"gyro\"", "<gyro name=\"gyro\" cutoff=\"1\""),
        (
            "<accelerometer name=\"accel\" site=\"site\"",
            "<accelerometer name=\"accel\" site=\"other_site\"",
        ),
    ] {
        let model = CompiledModel::from_xml_string(&ROTOR.replace(before, after)).unwrap();
        assert!(
            MujocoPhysics::from_model(model, bindings()).is_err(),
            "{after}"
        );
    }
    for flags in [
        MjtDisableBit::mjDSBL_ACTUATION,
        MjtDisableBit::mjDSBL_SENSOR,
    ] {
        let mut model = CompiledModel::from_xml_string(ROTOR).unwrap();
        model.opt_mut().disableflags = flags as i32;
        assert!(MujocoPhysics::from_model(model, bindings()).is_err());
    }
    let mut model = CompiledModel::from_xml_string(ROTOR).unwrap();
    model.opt_mut().disableactuator = 1;
    assert!(MujocoPhysics::from_model(model, bindings()).is_err());
    let mut physics = MujocoPhysics::from_xml_string(
        &ROTOR.replace(
            "<motor name=\"drive\"",
            "<motor name=\"drive\" group=\"-1\"",
        ),
        bindings(),
    )
    .unwrap();
    physics.set_joint_effort("shaft", 0.4).unwrap();
    physics.step(1_000_000).unwrap();
    near(
        physics.get_joint_state("shaft").unwrap().velocity_rad_s,
        0.02,
        1e-13,
    );
}

#[test]
fn multi_control_and_multi_output_prefixes_use_native_addresses() {
    let passive = r#"<body name="passive"><joint name="other" axis="0 0 1"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/></body>"#;
    let xml = ROTOR
        .replace("</worldbody>", &format!("{passive}</worldbody>"))
        .replace(
            "<actuator>",
            "<actuator><pid name=\"prefix\" joint=\"other\" input=\"pos vel\"/>",
        );
    let model = CompiledModel::from_xml_string(&xml).unwrap();
    let drive = model.name_to_id(MjtObj::mjOBJ_ACTUATOR, "drive").unwrap();
    assert_eq!((drive, model.actuator_ctrladr()[drive]), (1, 2));
    let mut physics = MujocoPhysics::from_model(model, bindings()).unwrap();
    physics.set_joint_effort("shaft", 0.4).unwrap();
    physics.step(1_000_000).unwrap();
    near(
        physics.get_joint_state("shaft").unwrap().velocity_rad_s,
        0.02,
        1e-13,
    );

    let xml = xml
        .replace(
            "<joint name=\"other\" axis=\"0 0 1\"/>",
            "<joint name=\"other\" type=\"ball\"/>",
        )
        .replace(
            "<pid name=\"prefix\" joint=\"other\" input=\"pos vel\"/>",
            "<orientation name=\"prefix\" joint=\"other\" input=\"quat\"/>",
        )
        .replace("<motor name=\"drive\"", "<motor name=\"drive\" gear=\"2\"");
    let model = CompiledModel::from_xml_string(&xml).unwrap();
    let drive = model.name_to_id(MjtObj::mjOBJ_ACTUATOR, "drive").unwrap();
    assert_eq!((drive, model.actuator_outadr()[drive]), (1, 3));
    assert!(MujocoPhysics::from_model(model, bindings()).is_err());
}

#[test]
fn solver_failure_keeps_diagnostic_time_and_freezes_until_reset() {
    // Native warnings write MUJOCO_LOG.TXT in cwd. Use the existing integration
    // test executable in an isolated directory; never change cwd for other tests.
    if std::env::var_os("SIM_MUJOCO_FAILURE_CHILD").is_none() {
        let directory = tempfile::tempdir().unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "solver_failure_keeps_diagnostic_time_and_freezes_until_reset",
                "--nocapture",
            ])
            .env("SIM_MUJOCO_FAILURE_CHILD", "1")
            .current_dir(directory.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    let mut physics = rotor();
    physics.set_joint_effort("shaft", 1e308).unwrap(); // Finite; native BADCTRL.
    let error = physics.step(1_000_000).unwrap_err();
    assert!(error.to_string().contains("mjWARN_BADCTRL"));
    assert!(physics.time_s().unwrap().is_finite());
    for operation in [
        physics.validate_step(1_000_000),
        physics.validate_steps(&[1_000_000]),
        physics.validate_joint_effort("shaft", 0.4),
        physics.get_joint_state("shaft").map(|_| ()),
        physics.get_body_pose("rotor").map(|_| ()),
        physics.get_imu("imu").map(|_| ()),
        physics.step(1_000_000),
        physics.set_joint_effort("shaft", 0.4),
    ] {
        assert!(operation
            .unwrap_err()
            .to_string()
            .contains("reset required"));
    }
    physics.reset().unwrap();
    assert_eq!(physics.time_s().unwrap(), 0.0);
    physics.set_joint_effort("shaft", 0.4).unwrap();
    // Native mju_isBad rejects theta=2e13 from this admitted million-second
    // Euler step immediately, before a subsequent step could warn BADQPOS.
    assert!(physics
        .step(1_000_000_000_000_000)
        .unwrap_err()
        .to_string()
        .contains("invalid target qpos"));
    assert!(physics.get_body_pose("rotor").is_err());
    physics.reset().unwrap();
    physics.step(1_000_000).unwrap();
    assert_eq!(
        physics.get_joint_state("shaft").unwrap(),
        JointState {
            position_rad: 0.0,
            velocity_rad_s: 0.0
        }
    );
}

#[test]
fn static_body_bad_pose_freezes_when_observed() {
    let mut model = CompiledModel::from_xml_string(
        r#"<mujoco><worldbody><body name="static" pos="1 0 0"/></worldbody></mujoco>"#,
    )
    .unwrap();
    model.body_pos_mut()[1][0] = f64::INFINITY;
    let mut physics = MujocoPhysics::from_model(model, MujocoBindings::default()).unwrap();
    assert!(physics
        .get_body_pose("static")
        .unwrap_err()
        .to_string()
        .contains("body pose"));
    assert!(physics
        .step(1_000_000)
        .unwrap_err()
        .to_string()
        .contains("reset required"));
}

#[test]
fn close_releases_owned_runtime_and_is_idempotent() {
    let mut physics = rotor();
    physics.close().unwrap();
    physics.close().unwrap();
    for operation in [
        physics.time_s().map(|_| ()),
        physics.reset(),
        physics.validate_step(1_000_000),
        physics.validate_steps(&[1_000_000]),
        physics.step(1_000_000),
        physics.step_seconds(0.001),
        physics.validate_joint_effort("shaft", 0.4),
        physics.set_joint_effort("shaft", 0.4),
        physics.get_joint_state("shaft").map(|_| ()),
        physics.get_body_pose("rotor").map(|_| ()),
        physics.get_imu("imu").map(|_| ()),
    ] {
        assert!(operation.unwrap_err().to_string().contains("closed"));
    }
}
