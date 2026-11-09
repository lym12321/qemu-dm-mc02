#![cfg(feature = "devices")]

use sim_core::{Session, StepContext};
use sim_host::{
    can::{CanBus, CanBusOptions, CanCallbacks, CanFrame, EndpointId},
    devices::dji::{
        protocol::{decode_current_command, encode_feedback},
        DeviceOptions, M3508Device, M3508,
    },
    physics::rotor::{Rotor, RotorOptions},
};
use std::{
    f64::consts::{PI, TAU},
    sync::Arc,
};

fn session() -> Session {
    Session::new(1, 0).unwrap()
}
fn step(id: u64, time: u64, dt: u64) -> StepContext {
    StepContext::new(1, id, time, dt).unwrap()
}
fn frame(id: u32, bytes: &[u8], time: u64) -> CanFrame {
    CanFrame::new(id, 0, None, bytes, time).unwrap()
}
fn close(actual: f64, expected: f64) {
    assert!((actual - expected).abs() < 1e-12, "{actual} != {expected}");
}

#[test]
fn independent_publisher_big_endian_command_and_feedback_vectors() {
    let bytes = [0x10, 0, 0xf0, 0, 0x40, 0, 0xc0, 0];
    for (group, ids) in [(0x200, 1..=4), (0x1ff, 5..=8)] {
        let command = frame(group, &bytes, 0);
        assert_eq!(
            ids.map(|id| decode_current_command(&command, id).unwrap().unwrap())
                .collect::<Vec<_>>(),
            [4096, -4096, 16384, -16384]
        );
    }
    assert_eq!(
        decode_current_command(&frame(0x1ff, &bytes, 0), 1).unwrap(),
        None
    );
    assert_eq!(
        encode_feedback(1, 4096, -1200, -4096, 25, 19).unwrap(),
        frame(0x201, &[0x10, 0, 0xfb, 0x50, 0xf0, 0, 25, 0], 19)
    );
    assert_eq!(
        encode_feedback(8, 8191, -32768, 32767, 255, 0)
            .unwrap()
            .data(),
        [0x1f, 0xff, 0x80, 0, 0x7f, 0xff, 0xff, 0]
    );
    assert!(encode_feedback(1, 8192, 0, 0, 25, 0).is_err());
}

#[test]
fn whole_group_rejection_does_not_extend_current_expiry() {
    let mut device = M3508Device::new(DeviceOptions::default()).unwrap();
    device.reset(session(), 0.0, 0.0).unwrap();
    device
        .receive(&frame(0x200, &[0x10, 0, 0, 0, 0, 0, 0, 0], 0))
        .unwrap();
    let plan = device
        .prepare_step(step(1, 40_000_000, 40_000_000))
        .unwrap();
    device
        .complete_step(&plan, 0.0, 0.0, &mut |_| Ok(()))
        .unwrap();
    for bad in [
        frame(0x200, &[0, 0, 0xbf, 0xff, 0, 0, 0, 0], 40_000_000),
        frame(0x200, &[0; 7], 40_000_000),
        CanFrame::new(0x200, 4, Some(8), vec![0; 8], 40_000_000).unwrap(),
    ] {
        device.receive(&bad).unwrap();
        assert_eq!(device.current_raw(), 4096);
    }
    let plan = device
        .prepare_step(step(2, 100_000_000, 60_000_000))
        .unwrap();
    device
        .complete_step(&plan, 0.0, 0.0, &mut |_| Ok(()))
        .unwrap();
    assert_eq!(
        device.state(),
        sim_host::devices::dji::DeviceState {
            current_raw: 0,
            rejected_commands: 3
        }
    );
}

#[test]
fn declared_rotor_integration_and_expiry_split_remain_distinct_when_torque_is_zero() {
    let mut rotor = Rotor::new(RotorOptions {
        initial_position: 2.0,
        initial_velocity: 1.0,
        ..Default::default()
    })
    .unwrap();
    rotor.step(0.01, 0.4).unwrap();
    close(rotor.state().0, 2.0116);
    close(rotor.state().1, 1.16);
    for (torque, initial, position, velocity) in [(0.02, 0.0, 0.28, 1.6), (0.0, 1.0, 0.084, 0.48)] {
        let mut motor = M3508::new(
            DeviceOptions {
                torque_per_amp: torque,
                ..Default::default()
            },
            Rotor::new(RotorOptions {
                initial_velocity: initial,
                ..Default::default()
            })
            .unwrap(),
        )
        .unwrap();
        motor.reset(session()).unwrap();
        motor
            .device
            .receive(&frame(0x200, &[0x40, 0, 0, 0, 0, 0, 0, 0], 0))
            .unwrap();
        let mut feedback = vec![];
        motor
            .step(step(1, 150_000_000, 150_000_000), &mut |frame| {
                feedback.push(frame);
                Ok(())
            })
            .unwrap();
        close(motor.rotor.state().0, position);
        close(motor.rotor.state().1, velocity);
        assert_eq!(feedback.len(), 1);
        assert_eq!(&feedback[0].data()[4..6], [0, 0]);
    }
}

#[test]
fn pending_plan_identity_finite_admission_and_failed_emission_preserve_partial_effects() {
    let mut first = M3508Device::new(DeviceOptions::default()).unwrap();
    first.reset(session(), 2.0, 3.0).unwrap();
    let mut second = M3508Device::new(DeviceOptions::default()).unwrap();
    second.reset(session(), 0.0, 0.0).unwrap();
    let plan = first.prepare_step(step(1, 1_000_000, 1_000_000)).unwrap();
    let other = second.prepare_step(step(1, 1_000_000, 1_000_000)).unwrap();
    assert_eq!((first.time_ns(), first.rotor_state()), (0, (2.0, 3.0)));
    assert!(first.receive(&frame(0x200, &[0; 8], 0)).is_err());
    assert!(first
        .complete_step(&other, 0.0, 0.0, &mut |_| Ok(()))
        .is_err());
    assert!(first
        .complete_step(&plan, f64::NAN, 0.0, &mut |_| Ok(()))
        .is_err());
    assert_eq!(first.time_ns(), 0);
    let primary: sim_core::Failure = Arc::new(std::io::Error::other("feedback send failed"));
    let error = first
        .complete_step(&plan, 4.0, 5.0, &mut |_| Err(primary.clone()))
        .unwrap_err();
    assert!(Arc::ptr_eq(&error, &primary));
    assert_eq!(
        (first.time_ns(), first.rotor_state()),
        (1_000_000, (4.0, 5.0))
    );
    assert!(first
        .complete_step(&plan, 4.0, 5.0, &mut |_| panic!("no replay"))
        .is_err());
    first.reset(Session::new(2, 0).unwrap(), 0.0, 0.0).unwrap();
    assert!(first
        .complete_step(&plan, 4.0, 5.0, &mut |_| panic!("stale plan"))
        .is_err());
}

#[test]
fn feedback_wrap_saturation_ties_even_and_legacy_live_rotor() {
    let mut motor = M3508::new(
        DeviceOptions {
            motor_id: 5,
            ..Default::default()
        },
        Rotor::new(RotorOptions {
            initial_position: -PI / 2.0,
            initial_velocity: -40.0 * PI,
            ..Default::default()
        })
        .unwrap(),
    )
    .unwrap();
    motor.reset(session()).unwrap();
    assert_eq!(
        motor.feedback().unwrap().data(),
        [0x18, 0, 0xfb, 0x50, 0, 0, 25, 0]
    );
    motor.rotor.step(0.0, 0.0).unwrap();
    let mut device = M3508Device::new(DeviceOptions::default()).unwrap();
    for (velocity, rpm) in [
        (f64::MAX, 32767i16),
        (-f64::MAX, -32768),
        (2.5 * PI / 30.0, 2),
    ] {
        device.reset(session(), 2.0 * TAU + PI, velocity).unwrap();
        let feedback = device.feedback().unwrap();
        assert_eq!(&feedback.data()[..2], [0x10, 0]);
        assert_eq!(&feedback.data()[2..4], rpm.to_be_bytes());
    }
}

struct Routes {
    motor: M3508,
    motor_id: EndpointId,
    received: Vec<CanFrame>,
}
impl CanCallbacks for Routes {
    fn receive(
        &mut self,
        destination: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> sim_host::Result<()> {
        if *destination == self.motor_id {
            self.motor
                .device
                .receive(frame)
                .map_err(|e| Arc::new(sim_host::Error::Callback(e.to_string())))
        } else {
            self.received.push(frame.clone());
            Ok(())
        }
    }
}

#[test]
fn actual_can_owner_delivers_command_and_completed_toy_feedback() {
    let host = EndpointId::new();
    let motor_id = EndpointId::new();
    let mut bus = CanBus::new(CanBusOptions::default()).unwrap();
    bus.attach(&host, "host").unwrap();
    bus.attach(&motor_id, "motor").unwrap();
    bus.reset(0).unwrap();
    let mut routes = Routes {
        motor: M3508::new(
            DeviceOptions::default(),
            Rotor::new(RotorOptions::default()).unwrap(),
        )
        .unwrap(),
        motor_id: motor_id.clone(),
        received: vec![],
    };
    routes.motor.reset(session()).unwrap();
    bus.send(&host, frame(0x200, &[0x40, 0, 0, 0, 0, 0, 0, 0], 0), None)
        .unwrap();
    bus.step(0, None, &mut routes).unwrap();
    routes
        .motor
        .step(step(1, 10_000_000, 10_000_000), &mut |feedback| {
            bus.send(&motor_id, feedback, None)
                .map_err(|e| -> sim_core::Failure { e })
        })
        .unwrap();
    bus.step(10_000_000, None, &mut routes).unwrap();
    assert_eq!(
        routes.received,
        [frame(0x201, &[0, 2, 0, 2, 0x40, 0, 25, 0], 10_000_000)]
    );
    close(routes.motor.rotor.state().0, 0.002);
    close(routes.motor.rotor.state().1, 0.2);
}
