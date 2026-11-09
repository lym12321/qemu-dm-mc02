#![cfg(feature = "scene")]

use sim_core::{EventPayload, SimulationOptions};
use sim_host::{
    apps::experiment::run_experiment,
    assertions::Status,
    config::{
        scene::{load_scene, parse_scene},
        yaml::parse_document,
    },
    runtime::HostEvent,
};
use std::{path::PathBuf, rc::Rc, sync::Arc};

#[test]
fn actual_host_scene_reports_observation_receipts_after_cleanup() {
    let source =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/can-faults/host-only.yaml");
    let scene = load_scene(source).unwrap();
    let result = run_experiment(&scene, SimulationOptions::default(), None).unwrap();
    assert!(result.passed());
    assert_eq!(result.delivered_inputs, 9);
    assert_eq!(result.end_ns, 30);
    assert_eq!(
        result
            .matches
            .iter()
            .map(|receipt| (
                receipt.assertion_index.unwrap(),
                receipt.source_time_ns.unwrap(),
                receipt.delivery_time_ns
            ))
            .collect::<Vec<_>>(),
        [(0, 1, 3), (1, 9, 11), (2, 10, 12), (3, 5, 17), (4, 19, 21)]
    );
}

#[test]
fn empty_completed_cut_expires_before_late_old_wire_metadata_arrives() {
    let document = parse_document(
        br#"
schema: sim/v1
runtime: {quantum: 10ns, duration: 30ns}
nodes:
  host: {kind: device.can-host}
  peer: {kind: device.can-host}
  bus: {kind: bus.can, delay_ns: 20}
links: [[host.can, bus.can], [peer.can, bus.can]]
experiment:
  inputs: [{at: 0ns, endpoint: host.tx, value: {id: 1, dlc: 1, data_hex: aa}}]
  assertions: [{before: 10ns, can_seen: {endpoint: host.can, id: 1}}]
"#,
    )
    .unwrap();
    let scene = parse_scene(PathBuf::from("/admitted/scene.yaml"), &document).unwrap();
    let result = run_experiment(&scene, SimulationOptions::default(), None).unwrap();
    assert_eq!(result.status, [Status::Fail]);
    assert_eq!(
        result.failures,
        ["assertion[1] can_seen host.can did not match before 10 ns"]
    );
    assert!(result.matches.is_empty());
}

#[test]
fn observer_failure_preserves_allocation_and_partial_committed_time_through_cleanup() {
    let source =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/can-faults/host-only.yaml");
    let scene = load_scene(source).unwrap();
    let primary: sim_core::Failure = Arc::new(std::io::Error::other("observer output failed"));
    let returned = primary.clone();
    let callback = Rc::new(
        move |event: &sim_core::Event<_, _>, _: &sim_core::Control<_, _>| {
            if matches!(
                &event.payload,
                EventPayload::Observation(HostEvent::CanSent { .. })
            ) {
                return Err(returned.clone());
            }
            Ok(())
        },
    );
    let result = run_experiment(&scene, SimulationOptions::default(), Some(callback)).unwrap_err();
    assert!(Arc::ptr_eq(&result.primary, &primary));
    // The input effect failed before its cut committed, preserving time0.
    assert_eq!(result.time_ns, 0);
}
