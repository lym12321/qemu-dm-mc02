#![cfg(all(feature = "simulation", feature = "physics"))]
use sim_core::{Result, Session};
use sim_host::worker::{
    engine::{EngineSample, NullEngine, WorkerEngine},
    graph::LegacyGraph,
    motor_protocol::{MotorAction, MotorCommand},
};
use std::{
    cell::RefCell,
    rc::Rc,
    time::{Duration, Instant},
};
fn command(action: MotorAction, time: u64) -> MotorCommand {
    MotorCommand {
        index: 0,
        action,
        timestamp_ns: time,
    }
}
#[test]
fn null_engine_uses_existing_rotor_and_enable_reset_policy() {
    let mut e = NullEngine::new(1, 10.0).unwrap();
    e.command(command(MotorAction::Legacy(2.0), 0)).unwrap();
    assert_eq!(e.step(0.01).unwrap().motors[0], (0.0, 0.0, 0.0));
    e.command(command(MotorAction::Enable(true), 0)).unwrap();
    let s = e.step(0.01).unwrap();
    assert_eq!(s.motors[0], (0.01, 1.0, 2.0));
    e.command(command(MotorAction::Reset, 0)).unwrap();
    assert_eq!(e.step(0.01).unwrap().motors[0], (0.0, 0.0, 0.0));
}
#[derive(Default)]
struct Calls {
    steps: Vec<(f64, f64)>,
    value: f64,
    resets: usize,
    closes: usize,
}
struct Probe(Rc<RefCell<Calls>>);
impl WorkerEngine for Probe {
    fn reset(&mut self) -> Result<()> {
        let mut c = self.0.borrow_mut();
        c.resets += 1;
        c.value = 0.0;
        Ok(())
    }
    fn command(&mut self, v: MotorCommand) -> Result<()> {
        if let MotorAction::Legacy(x) = v.action {
            self.0.borrow_mut().value = x;
        }
        Ok(())
    }
    fn step(&mut self, dt: f64) -> Result<EngineSample> {
        let mut c = self.0.borrow_mut();
        let value = c.value;
        c.steps.push((dt, value));
        Ok(EngineSample {
            imu: sim_host::signals::ImuSample {
                gyro_rad_s: [0.0; 3],
                accel_m_s2: [0.0, 0.0, 9.80665],
            },
            motors: vec![],
            source_time_ns: None,
        })
    }
    fn close(&mut self) -> Result<()> {
        self.0.borrow_mut().closes += 1;
        Ok(())
    }
}
#[test]
fn core_orders_commands_without_extra_physics_steps_and_reset_retains_owner() {
    let calls = Rc::new(RefCell::new(Calls::default()));
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut g = LegacyGraph::new(
        Box::new(Probe(calls.clone())),
        Session::new(1, 100).unwrap(),
        deadline,
        None,
    )
    .unwrap();
    g.submit(command(MotorAction::Legacy(8.0), 400)).unwrap();
    g.submit(command(MotorAction::Legacy(2.0), 200)).unwrap();
    g.submit(command(MotorAction::Legacy(3.0), 200)).unwrap();
    let dt = 1.0 / 333.0;
    g.advance(dt, deadline).unwrap();
    g.reach(300, deadline).unwrap();
    assert_eq!(calls.borrow().steps, vec![(dt, 0.0)]);
    g.advance(dt, deadline).unwrap();
    assert_eq!(calls.borrow().steps[1], (dt, 3.0));
    // The future command is cancelled, and the existing backend is reset.
    g.rebase(Session::new(2, 10).unwrap(), deadline).unwrap();
    assert_eq!((calls.borrow().resets, calls.borrow().closes), (2, 0));
    g.advance(dt, deadline).unwrap();
    assert_eq!(calls.borrow().steps[2], (dt, 0.0));
    g.close().unwrap();
    assert_eq!(calls.borrow().closes, 1);
}
#[cfg(feature = "mujoco")]
#[test]
fn native_mujoco_legacy_discovery_and_exact_float_dt() {
    use sim_host::worker::engine::MujocoEngine;
    use std::io::Write;
    let mut xml = tempfile::NamedTempFile::new().unwrap();
    write!(xml,"<mujoco><option gravity='0 0 0'/><worldbody><body><joint name='hinge' type='hinge' axis='0 0 1' damping='0'/><geom type='sphere' size='.1' mass='1'/><site name='imu'/></body></worldbody><actuator><motor name='motor' joint='hinge'/></actuator><sensor><gyro name='gyro' site='imu'/><accelerometer name='accel' site='imu'/></sensor></mujoco>").unwrap();
    let dt = 1.0 / 333.0;
    let mut e = MujocoEngine::new(xml.path(), 4, dt, 10.0).unwrap();
    e.command(command(MotorAction::Legacy(0.4), 0)).unwrap();
    e.command(command(MotorAction::Enable(true), 0)).unwrap();
    let s = e.step(dt).unwrap();
    assert_eq!(e.time_s().unwrap(), dt);
    assert_eq!(s.motors.len(), 1);
    // Sphere axial inertia = 2/5 m r² = .004 kg m².
    assert!((s.motors[0].1 - 100.0 * dt).abs() < 1e-12);
    assert!((s.imu.gyro_rad_s[2] - s.motors[0].1).abs() < 1e-12);
    e.reset().unwrap();
    assert_eq!(e.time_s().unwrap(), 0.0);
    assert_eq!(e.step(dt).unwrap().motors[0], (0.0, 0.0, 0.0));
    e.close().unwrap();
}
