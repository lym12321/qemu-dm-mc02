#![cfg(all(feature = "worker", feature = "devices"))]
use sim_core::Session;
use sim_host::{
    can::CanFrame,
    worker::can_graph::{CanGraph, CanProfile, RotorProfile},
};
use std::time::{Duration, Instant};
#[test]
fn transport_bridge_preserves_future_source_time_and_c620_feedback() {
    let deadline = Instant::now() + Duration::from_secs(2);
    let profile = CanProfile::Rotor(RotorProfile::default());
    let admitted = profile.admit(true, deadline, None).unwrap();
    let mut graph =
        CanGraph::new(&admitted, true, Session::new(9, 0).unwrap(), deadline, None).unwrap();
    graph
        .submit(
            "qemu",
            CanFrame::new(0x200, 0, None, vec![0x10, 0, 0, 0, 0, 0, 0, 0], 500).unwrap(),
        )
        .unwrap();
    graph.reach(499, deadline).unwrap();
    assert!(graph.take_outputs().unwrap().is_empty());
    graph.reach(500, deadline).unwrap();
    let out = graph.take_outputs().unwrap();
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].0, "socketcan");
    assert_eq!(out[0].1.timestamp_ns(), 500);
    assert_eq!(out[0].1.can_id(), 0x200);
    graph.reach(1_000_000, deadline).unwrap();
    let out = graph.take_outputs().unwrap();
    assert_eq!(out.len(), 2);
    for (destination, frame) in out {
        assert!(["qemu", "socketcan"].contains(&destination.as_str()));
        assert_eq!((frame.can_id(), frame.timestamp_ns()), (0x201, 1_000_000));
        assert_eq!(frame.data(), [0, 0, 0, 0, 0x10, 0, 25, 0]);
    }
    graph.close().unwrap();
}
#[test]
fn socketcan_ingress_is_not_echoed_to_itself_and_motor_ids_are_admitted_purely() {
    let deadline = Instant::now() + Duration::from_secs(2);
    let profile = CanProfile::Rotor(RotorProfile::default());
    let admitted = profile.admit(true, deadline, None).unwrap();
    let mut graph = CanGraph::new(
        &admitted,
        true,
        Session::new(1, 100).unwrap(),
        deadline,
        None,
    )
    .unwrap();
    graph
        .submit(
            "socketcan",
            CanFrame::new(0x123, 0, None, vec![7], 100).unwrap(),
        )
        .unwrap();
    graph.reach(100, deadline).unwrap();
    let out = graph.take_outputs().unwrap();
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].0, "qemu");
    assert_eq!(out[0].1.data(), [7]);
    graph.close().unwrap();
    assert!(CanProfile::Rotor(RotorProfile {
        motor_ids: vec![1, 1],
        ..Default::default()
    })
    .validate()
    .is_err());
}
#[cfg(feature = "mujoco")]
#[test]
fn native_four_wheel_accepts_self_contained_custom_xml_and_rejects_changed_dependencies() {
    use std::io::Write;
    let mut file = tempfile::NamedTempFile::new().unwrap();
    file.write_all(include_bytes!("../../../models/four_wheel.xml"))
        .unwrap();
    file.write_all(b"\n<!-- custom self-contained source -->\n")
        .unwrap();
    let profile = CanProfile::FourWheel {
        model: file.path().into(),
        delay_ns: 0,
        command_timeout_ns: 100_000_000,
        torque_per_amp: 0.02,
    };
    let deadline = Instant::now() + Duration::from_secs(3);
    let admitted = profile.admit(false, deadline, None).unwrap();
    let mut graph = CanGraph::new(
        &admitted,
        false,
        Session::new(10, 4000).unwrap(),
        deadline,
        None,
    )
    .unwrap();
    graph.reach(1_004_000, deadline).unwrap();
    let imu = graph.sample().unwrap();
    assert!(imu.accel_m_s2[2] > 9.0);
    graph.close().unwrap();
    file.write_all(b"<!-- source changed -->").unwrap();
    assert!(CanGraph::new(
        &admitted,
        false,
        Session::new(11, 4000).unwrap(),
        deadline,
        None
    )
    .err()
    .unwrap()
    .to_string()
    .contains("changed"));
    std::fs::write(
        file.path(),
        "<mujoco><include file='external.xml'/></mujoco>",
    )
    .unwrap();
    assert!(profile
        .validate()
        .unwrap_err()
        .to_string()
        .contains("includes/assets"));
}
