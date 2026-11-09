#![cfg(feature = "simulation")]
use sim_host::worker::motor_policy::{dm_mit_effort, legacy_motor_effort, MitCommand, MotorPolicy};
#[test]
fn shared_mit_law_and_legacy_enable_state_use_observed_radians_and_newton_metres() {
    let command = MitCommand {
        position: 3.0,
        speed: 5.0,
        kp: 2.0,
        kd: 0.5,
        torque: 1.0,
    };
    assert_eq!(dm_mit_effort(command, 1.0, 3.0, 10.0).unwrap(), 6.0);
    assert_eq!(dm_mit_effort(command, 1.0, 3.0, 4.0).unwrap(), 4.0);
    assert_eq!(legacy_motor_effort(-20.0, true, 10.0).unwrap(), -10.0);
    let mut policy = MotorPolicy::new(10.0).unwrap();
    policy.set_mit(command).unwrap();
    assert_eq!(policy.effort(1.0, 3.0).unwrap(), 0.0);
    policy.set_enabled(true);
    assert_eq!(policy.effort(1.0, 3.0).unwrap(), 6.0);
    policy.set_enabled(false);
    policy.set_enabled(true);
    assert_eq!(policy.command(), Some(command));
    policy.set_legacy(2.0).unwrap();
    assert!(policy.command().is_none());
    assert_eq!(policy.effort(0.0, 0.0).unwrap(), 2.0);
    policy.reset();
    assert!(!policy.enabled());
    assert_eq!(policy.effort(0.0, 0.0).unwrap(), 0.0);
    for limit in [0.0, f64::NAN, f64::INFINITY] {
        assert!(MotorPolicy::new(limit).is_err());
    }
    let command = MitCommand {
        kp: f64::NAN,
        ..command
    };
    assert!(policy.set_mit(command).is_err());
    assert!(policy.command().is_none());
}
