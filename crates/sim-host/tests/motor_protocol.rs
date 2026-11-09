#![cfg(feature = "simulation")]
use sim_host::{
    can::{CanFrame, FD, RTR},
    worker::{
        motor_policy::MotorPolicy,
        motor_protocol::{DmAdapter, DmOptions, MotorAction, MotorFeedback, MotorProtocol},
    },
};
fn input(id: u32, flags: u32, data: impl Into<Vec<u8>>) -> CanFrame {
    CanFrame::new(id, flags, None, data, 123).unwrap()
}
#[test]
fn canonical_dm_words_and_float_little_endian_reach_shared_policy() {
    let adapter = DmAdapter::new(DmOptions::default()).unwrap();
    let mut policy = MotorPolicy::new(10.0).unwrap();
    for (last, expected) in [
        (0xfc, MotorAction::Enable(true)),
        (0xfd, MotorAction::Enable(false)),
        (0xfb, MotorAction::Reset),
    ] {
        let mut data = vec![255; 8];
        data[7] = last;
        let c = adapter.decode(&input(1, 0, data)).unwrap();
        assert_eq!((c.index, c.action, c.timestamp_ns), (0, expected, 123));
        c.apply(&mut policy).unwrap();
    }
    let command = adapter
        .decode(&input(0x200, 0, 1.5f32.to_le_bytes()))
        .unwrap();
    assert_eq!(command.action, MotorAction::Legacy(1.5));
    command.apply(&mut policy).unwrap();
    policy.set_enabled(true);
    assert_eq!(policy.effort(0.0, 0.0).unwrap(), 1.5);
    let mit = adapter
        .decode(&input(1, 0, [0, 0, 0, 0, 0, 0, 0, 0]))
        .unwrap();
    let MotorAction::Mit(command) = mit.action else {
        panic!()
    };
    assert_eq!(
        (
            command.position,
            command.speed,
            command.kp,
            command.kd,
            command.torque
        ),
        (-12.5, -30.0, 0.0, 0.0, -10.0)
    );
    let mit = adapter.decode(&input(1, 0, [255; 8])).unwrap();
    let MotorAction::Mit(command) = mit.action else {
        panic!()
    };
    assert_eq!(
        (
            command.position,
            command.speed,
            command.kp,
            command.kd,
            command.torque
        ),
        (12.5, 30.0, 500.0, 5.0, 10.0)
    );
}
#[test]
fn unrepresentable_inputs_and_complete_id_collisions_are_rejected() {
    let adapter = DmAdapter::new(DmOptions::default()).unwrap();
    for (flags, data) in [(RTR, vec![0; 8]), (FD, vec![0; 8]), (0, vec![0; 7])] {
        assert!(adapter.decode(&input(1, flags, data)).is_none());
    }
    assert!(adapter
        .decode(&input(0x200, 0, f32::NAN.to_le_bytes()))
        .is_none());
    for options in [
        DmOptions {
            motor_count: 17,
            ..Default::default()
        },
        DmOptions {
            motor_map: vec![(0, 2, 0x20)],
            ..Default::default()
        },
        DmOptions {
            motor_map: vec![(0, 1, 0x11), (0, 2, 0x12)],
            ..Default::default()
        },
        DmOptions {
            t_max: f64::NAN,
            ..Default::default()
        },
    ] {
        assert!(DmAdapter::new(options).is_err());
    }
    let adapter = DmAdapter::new(DmOptions {
        protocol: MotorProtocol::Float,
        ..Default::default()
    })
    .unwrap();
    assert!(adapter.decode(&input(1, 0, [0; 8])).is_none());
}
#[test]
fn feedback_quantization_uses_fixed_literal_clamps_map_and_even_midpoint() {
    let adapter = DmAdapter::new(DmOptions {
        motor_count: 2,
        motor_map: vec![(1, 0x31, 0x41)],
        t_max: 2.0,
        ..Default::default()
    })
    .unwrap();
    let result = adapter
        .feedback(MotorFeedback {
            index: 1,
            position: 99.0,
            velocity: -99.0,
            effort: 9.0,
            timestamp_ns: 10,
        })
        .unwrap();
    assert_eq!(result.can_id(), 0x41);
    assert_eq!(result.data(), [1, 255, 255, 0, 15, 255, 25, 25]);
    assert_eq!(result.timestamp_ns(), 10);
    let result = adapter
        .feedback(MotorFeedback {
            index: 0,
            position: 0.0,
            velocity: 0.0,
            effort: 0.0,
            timestamp_ns: 0,
        })
        .unwrap();
    assert_eq!(result.data(), [1, 128, 0, 128, 8, 0, 25, 25]);
}
