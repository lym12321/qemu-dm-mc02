#![cfg(feature = "devices")]

use sim_core::{Error, Result, Session, StepContext};
use sim_host::{
    can::{CanBus, CanBusOptions, CanFrame, EndpointId},
    devices::dji::{
        joints::{M3508JointDriver, M3508JointPlant, M3508PlantBinding},
        DeviceOptions, M3508Device,
    },
    physics::{
        binding::{JointDrivePlan, JointDriver},
        JointPhysics, JointState,
    },
};
use std::{cell::RefCell, rc::Rc, sync::Arc};

fn session() -> Session {
    Session::new(7, 100).unwrap()
}
fn context() -> StepContext {
    StepContext::new(7, 1, 1_000_100, 1_000_000).unwrap()
}
fn state(position_rad: f64, velocity_rad_s: f64) -> JointState {
    JointState {
        position_rad,
        velocity_rad_s,
    }
}
fn device() -> Rc<RefCell<M3508Device>> {
    Rc::new(RefCell::new(
        M3508Device::new(DeviceOptions::default()).unwrap(),
    ))
}

#[test]
fn ideal_gearing_uses_only_original_plan_and_rejects_bad_target_before_protocol_commit() {
    let device = device();
    let frames = Rc::new(RefCell::new(vec![]));
    let output = frames.clone();
    let mut driver = M3508JointDriver::new(
        device.clone(),
        2.0,
        Box::new(move |frame| {
            output.borrow_mut().push(frame);
            Ok(())
        }),
    )
    .unwrap();
    assert_eq!(driver.max_abs_effort_nm(), 0.8);
    driver.reset(session(), state(0.25, -0.5)).unwrap();
    assert_eq!(device.borrow().rotor_state(), (0.5, -1.0));
    device
        .borrow_mut()
        .receive(&CanFrame::new(0x200, 0, None, [0x40, 0, 0, 0, 0, 0, 0, 0], 100).unwrap())
        .unwrap();
    let plan = driver.prepare_step(context(), state(0.25, -0.5)).unwrap();
    assert_eq!(plan.effort_nm(), 0.8);
    let forged = JointDrivePlan::new(context(), plan.active_until_ns(), plan.effort_nm()).unwrap();
    assert!(driver.complete_step(&forged, state(0.4, 0.6)).is_err());
    assert!(driver.complete_step(&plan, state(f64::MAX, 0.6)).is_err());
    assert_eq!(device.borrow().time_ns(), 100);
    assert!(frames.borrow().is_empty());
    let result = driver.complete_step(&plan, state(0.4, 0.6)).unwrap();
    assert_eq!(result.reached_time_ns, 1_000_100);
    assert_eq!(device.borrow().rotor_state(), (0.8, 1.2));
    assert_eq!(*frames.borrow(), [device.borrow().feedback().unwrap()]);
    assert!(driver.complete_step(&plan, state(0.4, 0.6)).is_err());
}

#[test]
fn feedback_failure_commits_protocol_once_and_cannot_replay_after_reset() {
    let device = device();
    let primary: sim_core::Failure = Arc::new(Error::State("feedback sink failed".into()));
    let error = primary.clone();
    let calls = Rc::new(RefCell::new(vec![]));
    let output = calls.clone();
    let mut driver = M3508JointDriver::new(
        device.clone(),
        1.0,
        Box::new(move |frame| -> Result<()> {
            output.borrow_mut().push(frame);
            Err(error.clone())
        }),
    )
    .unwrap();
    driver.reset(session(), state(0.0, 0.0)).unwrap();
    let plan = driver.prepare_step(context(), state(0.0, 0.0)).unwrap();
    assert!(Arc::ptr_eq(
        &driver.complete_step(&plan, state(0.5, 0.6)).unwrap_err(),
        &primary
    ));
    assert_eq!(
        (device.borrow().time_ns(), device.borrow().rotor_state()),
        (1_000_100, (0.5, 0.6))
    );
    assert!(driver.complete_step(&plan, state(0.5, 0.6)).is_err());
    assert_eq!(calls.borrow().len(), 1);
    driver
        .reset(Session::new(8, 0).unwrap(), state(0.0, 0.0))
        .unwrap();
    assert!(driver.complete_step(&plan, state(0.5, 0.6)).is_err());
    assert_eq!(calls.borrow().len(), 1);
}

#[test]
fn invalid_gearing_closed_device_and_conversion_overflow_reject_before_reset() {
    for ratio in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let device = device();
        assert!(M3508JointDriver::new(device.clone(), ratio, Box::new(|_| Ok(()))).is_err());
        assert_eq!(device.borrow().time_ns(), 0);
    }
    let device = device();
    let driver = M3508JointDriver::new(device.clone(), 2.0, Box::new(|_| Ok(()))).unwrap();
    assert!(driver.validate_joint_state(state(f64::MAX, 0.0)).is_err());
    device.borrow_mut().close();
    assert!(M3508JointDriver::new(device, 1.0, Box::new(|_| Ok(()))).is_err());
    let device = Rc::new(RefCell::new(
        M3508Device::new(DeviceOptions {
            torque_per_amp: f64::MAX / 40.0,
            ..Default::default()
        })
        .unwrap(),
    ));
    assert!(M3508JointDriver::new(device, 4.0, Box::new(|_| Ok(()))).is_err());
}

struct ExternalPhysics {
    calls: Rc<RefCell<Vec<&'static str>>>,
    close_error: Option<sim_core::Failure>,
}
impl JointPhysics for ExternalPhysics {
    fn reset(&mut self) -> Result<()> {
        self.calls.borrow_mut().push("reset");
        Ok(())
    }
    fn validate_step(&self, _: u64) -> Result<()> {
        Ok(())
    }
    fn validate_steps(&self, _: &[u64]) -> Result<()> {
        Ok(())
    }
    fn step(&mut self, _: u64) -> Result<()> {
        self.calls.borrow_mut().push("step");
        Ok(())
    }
    fn validate_joint_effort(&self, _: &str, _: f64) -> Result<()> {
        Ok(())
    }
    fn set_joint_effort(&mut self, _: &str, _: f64) -> Result<()> {
        self.calls.borrow_mut().push("control");
        Ok(())
    }
    fn get_joint_state(&self, _: &str) -> Result<JointState> {
        Ok(state(0.0, 0.0))
    }
    fn close(&mut self) -> Result<()> {
        self.calls.borrow_mut().push("close");
        self.close_error
            .as_ref()
            .map_or(Ok(()), |error| Err(error.clone()))
    }
}

#[test]
fn owning_plant_admits_bus_local_identity_and_closes_devices_even_if_physics_close_fails() {
    let first = CanBus::new(CanBusOptions::default()).unwrap();
    let second = CanBus::new(CanBusOptions::default()).unwrap();
    let calls = Rc::new(RefCell::new(vec![]));
    let a = device();
    let b = device();
    let bindings = vec![
        M3508PlantBinding::new(a.clone(), &first, "a", 1.0, Box::new(|_| Ok(()))),
        M3508PlantBinding::new(b.clone(), &first, "b", 1.0, Box::new(|_| Ok(()))),
    ];
    assert!(M3508JointPlant::new(
        ExternalPhysics {
            calls: calls.clone(),
            close_error: None
        },
        bindings
    )
    .is_err());
    assert_eq!(*calls.borrow(), ["close"]);
    assert!(!a.borrow().is_closed() && !b.borrow().is_closed());
    calls.borrow_mut().clear();
    let primary: sim_core::Failure = Arc::new(Error::State("external close failed".into()));
    let bindings = vec![
        M3508PlantBinding::new(a.clone(), &first, "a", 1.0, Box::new(|_| Ok(()))),
        M3508PlantBinding::new(b.clone(), &second, "b", 1.0, Box::new(|_| Ok(()))),
    ];
    let mut plant = M3508JointPlant::new(
        ExternalPhysics {
            calls: calls.clone(),
            close_error: Some(primary.clone()),
        },
        bindings,
    )
    .unwrap();
    assert!(calls.borrow().is_empty());
    plant.reset(session()).unwrap();
    assert!(Arc::ptr_eq(&plant.close().unwrap_err(), &primary));
    plant.close().unwrap();
    assert!(a.borrow().is_closed() && b.borrow().is_closed());
    assert_eq!(*calls.borrow(), ["reset", "close"]);
    assert_eq!((first.time_ns(), second.time_ns()), (0, 0));
}

#[test]
fn owning_plant_keeps_buses_borrowed_and_freezes_ingress_failure_until_reset() {
    let mut bus = CanBus::new(CanBusOptions::default()).unwrap();
    let host = EndpointId::new();
    bus.attach(&host, "host").unwrap();
    bus.send(&host, CanFrame::new(1, 0, None, [1], 999).unwrap(), None)
        .unwrap();
    let device = device();
    let calls = Rc::new(RefCell::new(vec![]));
    let mut plant = M3508JointPlant::new(
        ExternalPhysics {
            calls: calls.clone(),
            close_error: None,
        },
        vec![M3508PlantBinding::new(
            device.clone(),
            &bus,
            "a",
            1.0,
            Box::new(|_| Ok(())),
        )],
    )
    .unwrap();
    plant.reset(session()).unwrap();
    let wrong_time = CanFrame::new(0x200, 0, None, [0x40, 0, 0, 0, 0, 0, 0, 0], 101).unwrap();
    assert!(plant.ingress(&bus, &wrong_time).is_err());
    assert!(plant.step(context()).is_err());
    assert_eq!(*calls.borrow(), ["reset"]);
    plant.reset(Session::new(8, 200).unwrap()).unwrap();
    plant.close().unwrap();
    assert_eq!((bus.time_ns(), bus.pending()), (0, 1));
    bus.send(&host, CanFrame::new(2, 0, None, [2], 999).unwrap(), None)
        .unwrap();
    assert_eq!(bus.pending(), 2);
}
