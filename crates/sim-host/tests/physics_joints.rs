#![cfg(feature = "physics")]

use sim_core::{Error, Result, Session, StepContext, StepResult};
use sim_host::physics::{
    binding::{JointDriveBinding, JointDrivePlan, JointDriver},
    joints::JointExecution,
    participant::PhysicsParticipant,
    BodyPose, ImuSample, JointPhysics, JointState, NativePhysics,
};
use std::{cell::RefCell, collections::BTreeMap, rc::Rc, sync::Arc};

type Calls = Rc<RefCell<Vec<String>>>;
type States = Rc<RefCell<BTreeMap<String, JointState>>>;
fn error() -> sim_core::Failure {
    Arc::new(Error::State("backend failed".into()))
}
struct Backend {
    calls: Calls,
    states: States,
    time: f64,
    fail_step: bool,
    limit: f64,
    target_state: Option<JointState>,
}
impl JointPhysics for Backend {
    fn reset(&mut self) -> Result<()> {
        self.calls.borrow_mut().push("reset".into());
        self.time = 0.0;
        Ok(())
    }
    fn validate_step(&self, dt: u64) -> Result<()> {
        if dt == 0 {
            Err(error())
        } else {
            Ok(())
        }
    }
    fn validate_steps(&self, intervals: &[u64]) -> Result<()> {
        for dt in intervals {
            self.validate_step(*dt)?;
        }
        Ok(())
    }
    fn step(&mut self, dt: u64) -> Result<()> {
        self.calls.borrow_mut().push(format!("step:{dt}"));
        self.time += dt as f64 / 1e9;
        if let Some(state) = self.target_state {
            self.states.borrow_mut().insert("b".into(), state);
        }
        if self.fail_step {
            Err(error())
        } else {
            Ok(())
        }
    }
    fn validate_joint_effort(&self, name: &str, value: f64) -> Result<()> {
        if !self.states.borrow().contains_key(name)
            || !value.is_finite()
            || value.abs() > self.limit
        {
            Err(error())
        } else {
            Ok(())
        }
    }
    fn set_joint_effort(&mut self, name: &str, effort: f64) -> Result<()> {
        self.calls.borrow_mut().push(format!("set:{name}:{effort}"));
        Ok(())
    }
    fn get_joint_state(&self, name: &str) -> Result<JointState> {
        self.calls.borrow_mut().push(format!("get:{name}"));
        self.states.borrow().get(name).copied().ok_or_else(error)
    }
    fn close(&mut self) -> Result<()> {
        self.calls.borrow_mut().push("close".into());
        Ok(())
    }
}
impl NativePhysics for Backend {
    fn time_s(&self) -> Result<f64> {
        Ok(self.time)
    }
    fn get_body_pose(&self, _: &str) -> Result<BodyPose> {
        Err(error())
    }
    fn get_imu(&self, _: &str) -> Result<ImuSample> {
        Err(error())
    }
}
struct Driver {
    name: &'static str,
    calls: Calls,
    effort: f64,
    expiry_offset: u64,
    state_limit: f64,
    wrong_completion: bool,
}
impl JointDriver for Driver {
    fn max_abs_effort_nm(&self) -> f64 {
        1.0
    }
    fn validate_joint_state(&self, sample: JointState) -> Result<()> {
        if !sample.position_rad.is_finite() || sample.position_rad.abs() > self.state_limit {
            Err(error())
        } else {
            Ok(())
        }
    }
    fn reset(&mut self, _: Session, _: JointState) -> Result<()> {
        self.calls
            .borrow_mut()
            .push(format!("reset-driver:{}", self.name));
        Ok(())
    }
    fn prepare_step(&mut self, context: StepContext, _: JointState) -> Result<Rc<JointDrivePlan>> {
        self.calls
            .borrow_mut()
            .push(format!("prepare:{}", self.name));
        Ok(Rc::new(JointDrivePlan::new(
            context,
            context.t_sim_ns - context.dt_ns + self.expiry_offset.min(context.dt_ns),
            self.effort,
        )?))
    }
    fn complete_step(&mut self, plan: &JointDrivePlan, _: JointState) -> Result<StepResult<()>> {
        self.calls
            .borrow_mut()
            .push(format!("complete:{}", self.name));
        Ok(StepResult::reached(
            plan.context().t_sim_ns - u64::from(self.wrong_completion),
        ))
    }
}
struct Fixture {
    backend: Backend,
    bindings: Vec<JointDriveBinding>,
    calls: Calls,
    states: States,
}
fn fixture() -> Fixture {
    let calls = Rc::new(RefCell::new(vec![]));
    let states = Rc::new(RefCell::new(
        ["a", "b"]
            .into_iter()
            .map(|name| {
                (
                    name.into(),
                    JointState {
                        position_rad: 0.0,
                        velocity_rad_s: 0.0,
                    },
                )
            })
            .collect(),
    ));
    let backend = Backend {
        calls: calls.clone(),
        states: states.clone(),
        time: 0.0,
        fail_step: false,
        limit: 1.0,
        target_state: None,
    };
    let bindings = [("a", 0.2, 3), ("b", -0.4, 7)]
        .into_iter()
        .map(|(name, effort, expiry_offset)| {
            JointDriveBinding::new(
                name,
                Rc::new(RefCell::new(Driver {
                    name,
                    calls: calls.clone(),
                    effort,
                    expiry_offset,
                    state_limit: 100.0,
                    wrong_completion: false,
                })),
            )
            .unwrap()
        })
        .collect();
    Fixture {
        backend,
        bindings,
        calls,
        states,
    }
}

#[test]
fn all_plans_and_torques_precede_each_shared_step_and_all_samples_precede_feedback() {
    let Fixture {
        backend,
        bindings,
        calls,
        ..
    } = fixture();
    let mut execution = JointExecution::new(backend, bindings).unwrap();
    calls.borrow_mut().clear();
    execution.reset(Session::new(7, 100).unwrap()).unwrap();
    assert_eq!(
        *calls.borrow(),
        [
            "reset",
            "get:a",
            "get:b",
            "reset-driver:a",
            "reset-driver:b"
        ]
    );
    calls.borrow_mut().clear();
    let result = execution
        .step(StepContext::new(7, 1, 110, 10).unwrap())
        .unwrap();
    assert_eq!((result.reached_time_ns, result.diagnostics), (110, Some(3)));
    assert_eq!(
        *calls.borrow(),
        [
            "get:a",
            "get:b",
            "prepare:a",
            "prepare:b",
            "set:a:0.2",
            "set:b:-0.4",
            "step:3",
            "set:a:0",
            "set:b:-0.4",
            "step:4",
            "set:a:0",
            "set:b:0",
            "step:3",
            "get:a",
            "get:b",
            "complete:a",
            "complete:b",
        ]
    );
    assert_eq!(execution.time_ns(), 110);
    calls.borrow_mut().clear();
    execution.close().unwrap();
    execution.close().unwrap();
    assert_eq!(*calls.borrow(), ["close"]); // No borrowed driver or bus close.
}

#[test]
fn binding_maximum_and_initial_unit_admission_precede_reset_or_control() {
    let Fixture {
        mut backend,
        bindings,
        calls,
        ..
    } = fixture();
    backend.limit = 0.5; // Driver's admitted maximum would exceed the backend.
    assert!(JointExecution::new(backend, bindings).is_err());
    assert_eq!(*calls.borrow(), ["close"]);
    let Fixture {
        backend,
        bindings,
        calls,
        ..
    } = fixture();
    let duplicate = vec![bindings[0].clone(), bindings[0].clone()];
    assert!(JointExecution::new(backend, duplicate).is_err());
    assert_eq!(*calls.borrow(), ["close"]);
    let Fixture {
        backend,
        bindings,
        calls,
        ..
    } = fixture();
    let mut duplicate_driver = bindings[0].clone();
    duplicate_driver.joint = "b".into();
    assert!(JointExecution::new(backend, vec![bindings[0].clone(), duplicate_driver]).is_err());
    assert_eq!(*calls.borrow(), ["close"]);
    let Fixture {
        backend,
        bindings,
        calls,
        states,
    } = fixture();
    states.borrow_mut().get_mut("b").unwrap().position_rad = 101.0;
    assert!(JointExecution::new(backend, bindings).is_err());
    assert_eq!(*calls.borrow(), ["get:a", "get:b", "close"]);
}

#[test]
fn backend_failure_preserves_partial_effects_and_requires_reset_without_replay() {
    let Fixture {
        mut backend,
        bindings,
        calls,
        ..
    } = fixture();
    backend.fail_step = true;
    let mut execution = JointExecution::new(backend, bindings).unwrap();
    execution.reset(Session::new(7, 100).unwrap()).unwrap();
    calls.borrow_mut().clear();
    let context = StepContext::new(7, 1, 110, 10).unwrap();
    assert!(execution.step(context).is_err());
    assert_eq!(execution.time_ns(), 100);
    assert_eq!(
        *calls.borrow(),
        [
            "get:a",
            "get:b",
            "prepare:a",
            "prepare:b",
            "set:a:0.2",
            "set:b:-0.4",
            "step:3"
        ]
    );
    calls.borrow_mut().clear();
    assert!(execution.step(context).is_err());
    assert!(calls.borrow().is_empty());
    execution.reset(Session::new(8, 900).unwrap()).unwrap();
    assert!(execution.check_live().is_ok());
    assert_eq!(execution.time_ns(), 900);
}

#[test]
fn every_target_unit_check_precedes_any_completion_and_bad_completion_freezes() {
    let Fixture {
        mut backend,
        bindings,
        calls,
        ..
    } = fixture();
    backend.target_state = Some(JointState {
        position_rad: 101.0,
        velocity_rad_s: 0.0,
    });
    let mut execution = JointExecution::new(backend, bindings).unwrap();
    execution.reset(Session::new(7, 0).unwrap()).unwrap();
    // Initial samples still pass. Simulate the backend getter's target sample
    // failing by a native step callback, rather than implementing a fake solver.
    // A converted sample above the driver's supported range fails even finite.
    calls.borrow_mut().clear();
    assert!(execution
        .step(StepContext::new(7, 1, 10, 10).unwrap())
        .is_err());
    assert_eq!(
        &calls.borrow()[calls.borrow().len() - 2..],
        ["get:a", "get:b"]
    );
    assert!(!calls
        .borrow()
        .iter()
        .any(|call| call.starts_with("complete:")));
    assert!(execution.check_live().is_err());

    let Fixture {
        backend,
        mut bindings,
        calls,
        ..
    } = fixture();
    bindings[1].driver = Rc::new(RefCell::new(Driver {
        name: "b",
        calls: calls.clone(),
        effort: -0.4,
        expiry_offset: 7,
        state_limit: 100.0,
        wrong_completion: true,
    }));
    let mut execution = JointExecution::new(backend, bindings).unwrap();
    execution.reset(Session::new(7, 0).unwrap()).unwrap();
    assert!(execution
        .step(StepContext::new(7, 1, 10, 10).unwrap())
        .is_err());
    assert_eq!(execution.time_ns(), 0);
    assert!(execution.check_live().is_err());
}

#[test]
fn native_participant_keeps_large_session_epoch_separate_and_rejects_stale_steps() {
    let Fixture { backend, calls, .. } = fixture();
    let mut owner = PhysicsParticipant::new(backend).unwrap();
    let origin = (1 << 63) + 17;
    owner.reset(Session::new(7, origin).unwrap()).unwrap();
    calls.borrow_mut().clear();
    assert!(owner
        .step(StepContext::new(8, 1, origin + 1_000_001, 1_000_001).unwrap())
        .is_err());
    assert!(calls.borrow().is_empty());
    let result = owner
        .step(StepContext::new(7, 1, origin + 1_000_001, 1_000_001).unwrap())
        .unwrap();
    assert_eq!(result.reached_time_ns, origin + 1_000_001);
    assert_eq!(result.diagnostics, Some(0.001000001));
    assert_eq!(owner.physics().time_s().unwrap(), 0.001000001);
    owner.close().unwrap();
    owner.close().unwrap();
    assert!(owner.check_live().is_err());
}
