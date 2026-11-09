#![cfg(all(feature = "gazebo", feature = "devices"))]

use sim_core::{Session, StepContext};
use sim_host::{
    can::{CanBus, CanBusOptions, CanCallbacks, CanFrame, EndpointId},
    devices::dji::{
        joints::{M3508JointPlant, M3508PlantBinding},
        DeviceOptions, M3508Device,
    },
    physics::{
        gazebo::{GazeboBindings, GazeboPhysics},
        participant::PhysicsParticipant,
    },
};
use std::{cell::RefCell, rc::Rc};

#[derive(Default)]
struct Delivered(Vec<CanFrame>);
impl CanCallbacks for Delivered {
    fn receive(
        &mut self,
        _: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> sim_host::Result<()> {
        self.0.push(frame.clone());
        Ok(())
    }
}
fn near(actual: f64, expected: f64, tolerance: f64) {
    assert!(
        (actual - expected).abs() <= tolerance,
        "{actual} != {expected} within {tolerance}"
    );
}

#[test]
fn existing_native_participant_keeps_large_core_epoch_and_off_grid_failure_separate() {
    let path =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../models/gazebo_imu_rotor.sdf");
    let physics = GazeboPhysics::from_sdf_path(
        path,
        GazeboBindings {
            imus: vec![("imu".into(), "rotor".into(), "body".into(), "imu".into())],
            ..Default::default()
        },
        &[],
    )
    .unwrap();
    let mut participant = PhysicsParticipant::new(physics).unwrap();
    let origin = (1 << 63) + 17;
    participant.reset(Session::new(7, origin).unwrap()).unwrap();
    assert!(participant
        .step(StepContext::new(7, 1, origin + 1, 1).unwrap())
        .is_err());
    assert_eq!(participant.physics().time_ns().unwrap(), 0);
    assert_eq!(participant.time_ns(), origin);
    assert!(participant.check_live().is_err());
    participant.reset(Session::new(8, origin).unwrap()).unwrap();
    participant
        .step(StepContext::new(8, 1, origin + 2_000_000, 2_000_000).unwrap())
        .unwrap();
    assert_eq!(participant.time_ns(), origin + 2_000_000);
    assert_eq!(participant.physics().time_ns().unwrap(), 2_000_000);
    assert_eq!(
        participant.physics().get_imu("imu").unwrap().accel_m_s2,
        [0.0, 0.0, 9.80665]
    );
    participant.close().unwrap();
}

#[test]
fn actual_c620_joint_plant_uses_native_ticks_shared_expiries_and_borrowed_can_queue() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("motors.sdf");
    let original = include_str!("../../../models/gazebo_rotor.sdf");
    let start = original.find("<model name=\"rotor\">").unwrap();
    let end = original.find("</model>").unwrap() + 8;
    let model = &original[start..end];
    let models = format!(
        "{}{}",
        model.replace("name=\"rotor\"", "name=\"left\""),
        model.replace("name=\"rotor\"", "name=\"right\"")
    );
    std::fs::write(&path, original.replace(model, &models)).unwrap();
    let physics = GazeboPhysics::from_sdf_path(
        path,
        GazeboBindings {
            joints: vec![
                ("a".into(), "left".into(), "shaft".into()),
                ("b".into(), "right".into(), "shaft".into()),
            ],
            ..Default::default()
        },
        &[],
    )
    .unwrap();
    let origin = (1 << 63) + 17;
    let mut bus = CanBus::new(CanBusOptions::default()).unwrap();
    let host = EndpointId::new();
    let motor = EndpointId::new();
    bus.attach(&host, "host").unwrap();
    bus.attach(&motor, "plant").unwrap();
    bus.reset(origin).unwrap();
    let bus = Rc::new(RefCell::new(bus));
    let mut bindings = vec![];
    let mut devices = vec![];
    for (id, joint, gear, timeout) in [(1, "a", 2.0, 3_000_000), (2, "b", 1.0, 7_000_000)] {
        let device = Rc::new(RefCell::new(
            M3508Device::new(DeviceOptions {
                motor_id: id,
                command_timeout_ns: timeout,
                ..Default::default()
            })
            .unwrap(),
        ));
        let send = bus.clone();
        let endpoint = motor.clone();
        bindings.push(M3508PlantBinding::new(
            device.clone(),
            &bus.borrow(),
            joint,
            gear,
            Box::new(move |frame| {
                send.borrow_mut()
                    .send(&endpoint, frame, None)
                    .map_err(|error| error as sim_core::Failure)
            }),
        ));
        devices.push(device);
    }
    let mut plant = M3508JointPlant::new(physics, bindings).unwrap();
    plant.reset(Session::new(7, origin).unwrap()).unwrap();
    bus.borrow_mut()
        .send(
            &host,
            CanFrame::new(0x200, 0, None, [0x20, 0, 0xe0, 0, 0, 0, 0, 0], origin).unwrap(),
            None,
        )
        .unwrap();
    let mut delivered = Delivered::default();
    bus.borrow_mut().step(origin, None, &mut delivered).unwrap();
    plant.ingress(&bus.borrow(), &delivered.0[0]).unwrap();
    let result = plant
        .step(StepContext::new(7, 1, origin + 10_000_000, 10_000_000).unwrap())
        .unwrap();
    assert_eq!(result.diagnostics, Some(3));
    // alpha=tau/I=.4/.02=20 and -.2/.02=-10 until3/7ms, then coast.
    // Continuous theta=.00051/-.000455; permit one native1ms integration tick.
    for (joint, position, velocity) in [("a", 0.00051, 0.06_f64), ("b", -0.000455, -0.07)] {
        let state = plant.get_joint_state(joint).unwrap();
        near(state.position_rad, position, velocity.abs() * 0.0011);
        near(state.velocity_rad_s, velocity, 1e-10);
    }
    assert_eq!(plant.time_ns(), origin + 10_000_000);
    assert_eq!(plant.time_s().unwrap(), 0.01);
    plant.close().unwrap();
    assert!(devices.iter().all(|device| device.borrow().is_closed()));
    assert_eq!(
        (bus.borrow().time_ns(), bus.borrow().pending()),
        (origin, 2)
    );
    let mut feedback = Delivered::default();
    bus.borrow_mut()
        .step(origin + 10_000_000, None, &mut feedback)
        .unwrap();
    assert_eq!(
        feedback.0,
        [
            CanFrame::new(
                0x201,
                0,
                None,
                [0, 1, 0, 1, 0, 0, 25, 0],
                origin + 10_000_000
            )
            .unwrap(),
            CanFrame::new(
                0x202,
                0,
                None,
                [0x1f, 0xff, 0xff, 0xff, 0, 0, 25, 0],
                origin + 10_000_000
            )
            .unwrap(),
        ]
    );
    bus.borrow_mut().close().unwrap();
}
