#![cfg(feature = "scene")]

use sim_core::{EventPayload, SimulationOptions, SimulationState};
use sim_host::{
    assertions::{Evaluator, Observation, ObservationData, Status},
    config::{
        scene::{load_scene, parse_scene},
        yaml::parse_document,
    },
    runtime::HostEvent,
};
use std::{cell::RefCell, path::PathBuf, rc::Rc};

#[test]
fn existing_can_fault_scene_runs_through_compiled_factories_and_shared_assertions() {
    let source =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/can-faults/host-only.yaml");
    let scene = load_scene(source).unwrap();
    let experiment = scene.experiment.as_ref().unwrap();
    let evaluator = Rc::new(RefCell::new(
        Evaluator::new(
            experiment
                .assertions
                .iter()
                .map(|value| value.assertion.clone())
                .collect(),
            scene.duration_ns,
            0,
        )
        .unwrap(),
    ));
    let mut simulation = scene.simulation(SimulationOptions::default()).unwrap();
    let observed = evaluator.clone();
    simulation.set_observer(Some(Rc::new(move |event, _| {
        if let EventPayload::Observation(HostEvent::CanDelivery { frame, .. }) = &event.payload {
            let endpoint = format!(
                "{}.{}",
                event.source_node,
                event.source_port.as_ref().unwrap()
            );
            observed
                .borrow_mut()
                .observe(
                    &[],
                    &[Observation {
                        endpoint: &endpoint,
                        timestamp_ns: event.mapped_time_ns,
                        data: ObservationData::Can(frame),
                    }],
                    event.delivery_time_ns,
                    true,
                )
                .map_err(|e| -> sim_core::Failure { e })?;
        }
        Ok(())
    })));
    simulation.start(None).unwrap();
    for input in &experiment.inputs {
        simulation
            .schedule_input(
                input.endpoint.clone(),
                input.payload.clone(),
                Some(input.at_ns),
            )
            .unwrap();
    }
    simulation.run_until(scene.duration_ns, None).unwrap();
    simulation.close().unwrap();
    assert_eq!(simulation.state(), SimulationState::Closed);
    assert_eq!(
        evaluator.borrow_mut().finish(30).unwrap(),
        [Status::Pass; 5]
    );
    let deliveries: Vec<_> = simulation
        .events()
        .into_iter()
        .filter_map(|event| {
            if let EventPayload::Observation(HostEvent::CanDelivery {
                destination,
                frame,
                due_ns,
                dropped,
                ..
            }) = event.payload
            {
                Some((frame.data()[0], destination, due_ns, dropped))
            } else {
                None
            }
        })
        .collect();
    // Fixed independent consequences of the public example: bb keeps the
    // previously admitted 12ns delay after switching to selective; drop uses
    // the active policy at actual delivery, only for host -> peer ID0x321.
    let expected = [
        (0xaa, "peer.can", 3, false),
        (0xaa, "tap.can", 3, false),
        (0xcc, "peer.can", 10, true),
        (0xcc, "tap.can", 10, false),
        (0xdd, "host.can", 11, false),
        (0xdd, "peer.can", 11, false),
        (0xee, "peer.can", 12, false),
        (0xee, "tap.can", 12, false),
        (0xbb, "peer.can", 17, true),
        (0xbb, "tap.can", 17, false),
        (0xff, "peer.can", 21, false),
        (0xff, "tap.can", 21, false),
    ]
    .map(|(byte, destination, due, drop)| (byte, destination.to_owned(), due, drop));
    assert_eq!(deliveries, expected);
}

#[test]
fn scene_admission_rejects_bad_options_inputs_and_observation_owners_before_start() {
    let scene = "schema: sim/v1\nnodes:\n  host: {kind: device.can-host}\n  bus: {kind: bus.can}\nlinks: [[host.can, bus.can]]\n";
    let source = PathBuf::from("/no/resources/scene.yaml");
    let parsed = parse_scene(source.clone(), &parse_document(scene.as_bytes()).unwrap()).unwrap();
    let simulation = parsed.simulation(SimulationOptions::default()).unwrap();
    assert_eq!(simulation.state(), SimulationState::New);
    for invalid in [
        scene.replace("kind: device.can-host", "kind: device.can-host, extra: 1"),
        format!("{scene}experiment:\n  inputs: [{{at: 1ns, endpoint: host.tx, value: {{id: 1, dlc: true, data_hex: aa}}}}]\n"),
        format!("{scene}experiment:\n  assertions: [{{before: 1ns, can_seen: {{endpoint: bus.can, id: 1}}}}]\n"),
        format!("{scene}experiment:\n  inputs: [{{at: 1ns, endpoint: host.tx, origin: 'marker:absent', value: {{id: 1, dlc: 1, data_hex: aa}}}}]\n"),
        scene.replace("[[host.can, bus.can]]", "[[host.tx, bus.can]]"),
        #[cfg(feature = "devices")]
        "schema: sim/v1\nnodes:\n  motor: {kind: device.dji-m3508, motor_id: 1, plant: fixed-rotor}\nlinks: []\n".to_string(),
        #[cfg(feature = "devices")]
        "schema: sim/v1\nnodes:\n  motor: {kind: device.dji-m3508, motor_id: 1, plant: joint}\nlinks: []\n".to_string(),
    ] {
        assert!(parse_scene(source.clone(), &parse_document(invalid.as_bytes()).unwrap()).is_err(), "{invalid}");
    }
}

#[cfg(all(target_os = "linux", feature = "machine"))]
#[test]
fn snapshot_assertions_and_binary_uart_admit_without_native_resources() {
    let source = PathBuf::from("/no/resources/scene.yaml");
    let text = "schema: sim/v1\nruntime: {duration: 1ms}\nnodes:\n  main: {kind: machine.qemu, firmware: missing.elf, uarts: [uart1]}\nexperiment:\n  inputs: [{at: 0ns, endpoint: main.uart1, value: !!binary AP8=}]\n  assertions:\n    - {before: 1ms, digital_equals: {endpoint: main.gpio.B0, value: 1}}\n    - {before: 1ms, digital_equals: {endpoint: main.gpio.B0.out, value: 1}}\n    - {before: 1ms, digital_equals: {endpoint: main.key, value: 1}}\n    - {before: 1ms, digital_equals: {endpoint: main.led, value: 0}}\n";
    let scene = parse_scene(source.clone(), &parse_document(text.as_bytes()).unwrap()).unwrap();
    assert_eq!(scene.experiment.as_ref().unwrap().assertions.len(), 4);
    assert_eq!(
        scene.experiment.unwrap().inputs[0].payload,
        sim_host::runtime::HostInput::Machine(sim_host::profile::MachineInput::Bytes(vec![0, 255]))
    );
    let invalid = text.replace("main.gpio.B0,", "main.gpio.B16,");
    assert!(parse_scene(source, &parse_document(invalid.as_bytes()).unwrap()).is_err());
}
