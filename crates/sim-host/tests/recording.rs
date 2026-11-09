#![cfg(all(feature = "scene", feature = "trace"))]

use sim_core::SimulationOptions;
use sim_host::{
    apps::recording::{replay_experiment, run_recorded},
    config::scene::load_scene,
    event_json::result_json,
    trace::{iter_events, TraceEvent, TraceWriter},
};
use std::{
    cell::RefCell,
    io::{self, Write},
    path::PathBuf,
    rc::Rc,
    sync::Arc,
};

#[derive(Clone, Default)]
struct Sink(Rc<RefCell<Vec<u8>>>);
impl Write for Sink {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.borrow_mut().extend(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn scene() -> sim_host::config::scene::Scene {
    load_scene(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/can-faults/host-only.yaml"),
    )
    .unwrap()
}
fn recorded() -> (Vec<u8>, serde_json::Value) {
    let sink = Sink::default();
    let result = run_recorded(&scene(), SimulationOptions::default(), sink.clone(), None).unwrap();
    let bytes = sink.0.borrow().clone();
    (bytes, result_json(&result))
}
#[test]
fn actual_scene_recording_replays_same_receipts_and_can_replace_assertions() {
    let (bytes, result) = recorded();
    assert_eq!(
        result_json(&replay_experiment(&mut bytes.as_slice(), None).unwrap()),
        result
    );
    let replacements = sim_host::config::yaml::parse_document(
        br#"assertions:
- before: 4ns
  can_seen: {endpoint: host.can, id: 801, data_hex: aa}
- before: 4ns
  can_seen: {endpoint: peer.can, id: 123}
"#,
    )
    .unwrap();
    let replay = replay_experiment(&mut bytes.as_slice(), Some(&replacements)).unwrap();
    assert_eq!(
        replay.status,
        [
            sim_host::assertions::Status::Pass,
            sim_host::assertions::Status::Fail
        ]
    );
    assert_eq!(replay.matches.len(), 1);
    assert_eq!(replay.matches[0].mapped_time_ns, 1);
    assert_eq!(replay.matches[0].delivery_time_ns, 3);
}
fn rewrite(bytes: &[u8], mut change: impl FnMut(&mut Vec<(u64, serde_json::Value)>)) -> Vec<u8> {
    let mut input = bytes;
    let mut values = iter_events(&mut input)
        .map(|event| {
            let event = event.unwrap();
            (
                event.timestamp_ns(),
                serde_json::from_slice(event.payload()).unwrap(),
            )
        })
        .collect::<Vec<_>>();
    change(&mut values);
    let mut output = vec![];
    let mut writer = TraceWriter::new(&mut output).unwrap();
    for (time, value) in values {
        writer
            .append(
                &TraceEvent::new(
                    time,
                    "simulation",
                    "experiment",
                    "sim/experiment/2",
                    serde_json::to_vec(&value).unwrap(),
                    "simulation",
                )
                .unwrap(),
            )
            .unwrap();
    }
    writer.finish().unwrap();
    drop(writer);
    output
}
#[test]
fn completion_stays_provisional_until_physical_eof_and_lifecycle_are_valid() {
    let (bytes, _) = recorded();
    let mut trailing = bytes.clone();
    trailing.push(0);
    assert!(replay_experiment(&mut trailing.as_slice(), None).is_err());
    let no_completion = rewrite(&bytes, |values| {
        values.pop();
    });
    assert!(replay_experiment(&mut no_completion.as_slice(), None).is_err());
    let mut empty_scene = scene();
    empty_scene.topology.links.clear();
    empty_scene.topology.nodes.retain(|n| n.name == "can0");
    empty_scene.topology.nodes[0].options =
        sim_host::runtime::HostOptions::CanBus(Default::default());
    // Plain scenes can be recorded without declaring inputs or assertions.
    empty_scene.experiment = None;
    let sink = Sink::default();
    run_recorded(
        &empty_scene,
        SimulationOptions::default(),
        sink.clone(),
        None,
    )
    .unwrap();
    let no_empty_cut = rewrite(&sink.0.borrow(), |values| {
        let index = values
            .iter()
            .position(|(_, v)| v["kind"] == "cut" && v["data"] == 10)
            .unwrap();
        values.remove(index);
        for (ordinal, (_, value)) in values.iter_mut().enumerate() {
            value["ordinal"] = serde_json::json!(ordinal);
        }
        let completion = &mut values.last_mut().unwrap().1["data"];
        completion["cuts"] = serde_json::json!(completion["cuts"].as_u64().unwrap() - 1);
        completion["records"] = serde_json::json!(completion["records"].as_u64().unwrap() - 1);
        completion["summary"]["value"]["cuts_total"] = completion["cuts"].clone();
    });
    // The counts are self-consistent; the missing sample cut creates a runtime
    // quantum gap that the real replay consumer must still reject.
    assert!(replay_experiment(&mut no_empty_cut.as_slice(), None).is_err());
}
#[test]
fn external_observer_failure_leaves_an_incomplete_trace_and_keeps_primary() {
    let sink = Sink::default();
    let primary: sim_core::Failure = Arc::new(io::Error::other("output consumer failed"));
    let returned = primary.clone();
    let error = run_recorded(
        &scene(),
        SimulationOptions::default(),
        sink.clone(),
        Some(Rc::new(move |_, _| Err(returned.clone()))),
    )
    .unwrap_err();
    assert!(Arc::ptr_eq(&error.primary, &primary));
    assert!(replay_experiment(&mut sink.0.borrow().as_slice(), None).is_err());
    assert!(!sink
        .0
        .borrow()
        .windows(b"completion".len())
        .any(|v| v == b"completion"));
}
#[test]
fn pinned_python_experiment_retains_can_faults_and_complete_receipts() {
    let mut input = include_bytes!("fixtures/python_can_experiment_v2.mcap").as_slice();
    let result = replay_experiment(&mut input, None).unwrap();
    assert_eq!(
        result_json(&result),
        serde_json::from_slice::<serde_json::Value>(include_bytes!(
            "fixtures/python_can_experiment_v2.json"
        ))
        .unwrap()
    );
    assert_eq!(result_json(&result), recorded().1);
}

#[test]
fn python_machine_ready_fixture_replays_with_only_scene_and_trace_features() {
    let mut input = include_bytes!("fixtures/python_ready_experiment_v2.mcap").as_slice();
    let result = replay_experiment(&mut input, None).unwrap();
    assert_eq!(
        result_json(&result),
        serde_json::from_slice::<serde_json::Value>(include_bytes!(
            "fixtures/python_ready_experiment_v2.json"
        ))
        .unwrap()
    );
}

#[test]
fn recorded_uart_assertion_keeps_literal_text_in_the_shared_grammar() {
    let document = sim_host::config::yaml::parse_document(
        br#"
schema: sim/v1
runtime: {quantum: 1ns, duration: 3ns}
nodes:
  echo: {kind: device.uart-echo}
  peer: {kind: device.uart-echo}
  uart: {kind: net.uart}
links: [[echo.uart, uart.uart], [peer.uart, uart.uart]]
experiment:
  assertions: [{before: 3ns, uart_contains: {endpoint: echo.uart, text: "READY\n"}}]
"#,
    )
    .unwrap();
    let scene =
        sim_host::config::scene::parse_scene(PathBuf::from("/admitted/uart.yaml"), &document)
            .unwrap();
    let sink = Sink::default();
    let live = run_recorded(&scene, SimulationOptions::default(), sink.clone(), None).unwrap();
    let replay = replay_experiment(&mut sink.0.borrow().as_slice(), None).unwrap();
    assert_eq!(result_json(&replay), result_json(&live));
    assert_eq!(
        replay.failures,
        ["assertion[1] uart_contains echo.uart did not match before 3 ns"]
    );
}
