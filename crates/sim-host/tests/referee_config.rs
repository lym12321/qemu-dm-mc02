#![cfg(all(feature = "referee", feature = "scene"))]
use sim_host::{
    apps::experiment::run_experiment,
    config::{
        scene::parse_scene,
        yaml::{parse_document, Value},
    },
    devices::referee::{
        config::{parse_events, parse_options},
        RefereeEvent,
    },
};
use std::path::PathBuf;
const CONFIG: &str = include_str!("fixtures/referee_options.yaml");
fn scene() -> sim_host::config::scene::Scene {
    let options = CONFIG
        .lines()
        .map(|line| format!("    {line}\n"))
        .collect::<String>();
    let document = format!(
        "schema: sim/v1\nruntime: {{quantum: 50ms, duration: 35ms}}\nnodes:\n  referee:\n    kind: device.referee\n{options}  wire: {{kind: net.uart}}\n  peer: {{kind: device.uart-echo}}\nlinks: [[referee.uart, wire.uart], [peer.uart, wire.uart]]\nexperiment:\n  inputs: [{{at: 0ns, endpoint: referee.enable, value: true}}]\n"
    );
    parse_scene(
        PathBuf::from("/admitted/referee.yaml"),
        &parse_document(document.as_bytes()).unwrap(),
    )
    .unwrap()
}
#[test]
fn domain_configuration_preserves_old_grammar_and_rejects_invalid_plain_values() {
    let configuration = parse_document(CONFIG.as_bytes()).unwrap();
    let options = parse_options(&configuration, "referee").unwrap();
    assert_eq!(
        (options.period_ns, options.configuration.events.len()),
        (100_000_000, 4)
    );
    assert!(matches!(
        options.configuration.events[0].value,
        RefereeEvent::Hit(_)
    ));
    for text in [
        CONFIG.replace("robot_id: 3", "robot_id: true"),
        CONFIG.replace("power_outputs: 5", "power_outputs: 8"),
        CONFIG.replace("countdown_s: 3", "countdown_s: 0"),
        format!("{CONFIG}\nperiod: 999us\n"),
        format!("{CONFIG}\nunknown: true\n"),
    ] {
        assert!(parse_options(&parse_document(text.as_bytes()).unwrap(), "referee").is_err());
    }
    for text in [
        "[{offset_ns: 1, hit: {damage_hp: 1}, shot: {heat_17mm: 0, heat_42mm: 0}}]",
        "[{offset_ns: 2, hit: {damage_hp: 1}}, {offset_ns: 1, hit: {damage_hp: 2}}]",
    ] {
        assert!(parse_events(&parse_document(text.as_bytes()).unwrap(), "events").is_err());
    }
    assert!(parse_events(&Value::Sequence(vec![]), "events")
        .unwrap()
        .is_empty());
}
#[test]
fn shared_scene_parser_and_experiment_run_the_compiled_referee_factory() {
    let result = run_experiment(&scene(), Default::default(), None).unwrap();
    assert!(result.passed());
    assert_eq!((result.delivered_inputs, result.end_ns), (1, 35_000_000));
}
#[cfg(feature = "trace")]
#[test]
fn actual_referee_event_trace_replays_without_native_io() {
    use sim_host::{
        apps::recording::{replay_experiment, run_recorded},
        event_json::result_json,
    };
    use std::io::Read;
    let mut file = tempfile::tempfile().unwrap();
    let result = run_recorded(
        &scene(),
        Default::default(),
        file.try_clone().unwrap(),
        None,
    )
    .unwrap();
    use std::io::{Seek, SeekFrom};
    file.seek(SeekFrom::Start(0)).unwrap();
    let mut bytes = vec![];
    file.read_to_end(&mut bytes).unwrap();
    let replay = replay_experiment(&mut bytes.as_slice(), None).unwrap();
    assert_eq!(result_json(&result), result_json(&replay));
}
