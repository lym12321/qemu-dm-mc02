#![cfg(feature = "scene")]

use serde_json::Value;
use std::{fs, path::PathBuf, process::Command};

fn executable() -> Command {
    Command::new(env!("CARGO_BIN_EXE_sim"))
}
fn example() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/can-faults/host-only.yaml")
}

#[test]
fn actual_cli_validates_runs_reports_failure_and_refuses_existing_outputs() {
    let directory = tempfile::tempdir().unwrap();
    let report = directory.path().join("result.json");
    let validated = executable()
        .arg("validate")
        .arg(example())
        .output()
        .unwrap();
    assert!(
        validated.status.success(),
        "{}",
        String::from_utf8_lossy(&validated.stderr)
    );
    let run = executable()
        .arg("run")
        .arg(example())
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();
    assert!(
        run.status.success(),
        "{}",
        String::from_utf8_lossy(&run.stderr)
    );
    let contents = fs::read(&report).unwrap();
    let value: Value = serde_json::from_slice(&contents).unwrap();
    assert_eq!(value["status"], "passed");
    assert_eq!(
        value["result"]["status"],
        serde_json::json!(vec!["pass"; 5])
    );
    assert_eq!(value["result"]["matches"][3]["source_time_ns"], 5);
    assert_eq!(value["result"]["matches"][3]["delivery_time_ns"], 17);
    let occupied = executable()
        .arg("run")
        .arg(example())
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();
    assert_eq!(occupied.status.code(), Some(1));
    assert!(occupied.stdout.is_empty());
    assert_eq!(fs::read(&report).unwrap(), contents);

    let source = directory.path().join("unmatched.yaml");
    fs::write(
        &source,
        fs::read_to_string(example()).unwrap().replace(
            "before: 23ns, can_seen: {endpoint: host.can, id: 0x321, data_hex: ff}",
            "before: 23ns, can_seen: {endpoint: host.can, id: 0x321, data_hex: fe}",
        ),
    )
    .unwrap();
    let failed_report = directory.path().join("failed.json");
    let failed = executable()
        .arg("run")
        .arg(source)
        .arg("--report")
        .arg(&failed_report)
        .output()
        .unwrap();
    assert_eq!(failed.status.code(), Some(1));
    let failure: Value = serde_json::from_slice(&fs::read(failed_report).unwrap()).unwrap();
    assert_eq!(failure["status"], "failed");
    assert_eq!(failure["result"]["status"][4], "fail");
    assert_eq!(failure["result"]["end_ns"], 30);
}

#[cfg(unix)]
#[test]
fn terminal_signal_unwinds_empty_cuts_and_publishes_only_failure() {
    use std::{
        io::{BufRead, BufReader},
        process::Stdio,
        time::Duration,
    };
    use wait_timeout::ChildExt;
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("long.yaml");
    let report = directory.path().join("interrupted.json");
    fs::write(&source, b"schema: sim/v1\nruntime: {duration: 1s, quantum: 1ns}\nnodes:\n  host: {kind: device.can-host}\n  peer: {kind: device.can-host}\n  bus: {kind: bus.can}\nlinks: [[host.can, bus.can], [peer.can, bus.can]]\nexperiment:\n  inputs: [{at: 0ns, endpoint: host.tx, value: {id: 1, dlc: 1, data_hex: aa}}]\n").unwrap();
    let mut child = executable()
        .arg("run")
        .arg(source)
        .arg("--report")
        .arg(&report)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap());
    let mut first = String::new();
    output.read_line(&mut first).unwrap();
    if serde_json::from_str::<Value>(&first).is_err() {
        let result = child.wait_with_output().unwrap();
        panic!(
            "CLI did not produce an event: {first}; {}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
    assert!(Command::new("kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .unwrap()
        .success());
    let status = match child.wait_timeout(Duration::from_secs(5)).unwrap() {
        Some(status) => status,
        None => {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("CLI did not unwind after SIGTERM");
        }
    };
    assert_eq!(status.code(), Some(130));
    let value: Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    assert_eq!(value["status"], "failed");
    assert_eq!(value["error"], "simulation interrupted");
    assert!(value["time_ns"].as_u64().unwrap() < 1_000_000_000);
}

#[cfg(feature = "trace")]
#[test]
fn cli_record_replay_replacement_and_container_exhaustion() {
    let directory = tempfile::tempdir().unwrap();
    let trace = directory.path().join("events.mcap");
    let report = directory.path().join("live.json");
    let replay_report = directory.path().join("replay.json");
    let live = executable()
        .arg("run")
        .arg(example())
        .arg("--trace")
        .arg(&trace)
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();
    assert!(
        live.status.success(),
        "{}",
        String::from_utf8_lossy(&live.stderr)
    );
    let replay = executable()
        .arg("replay")
        .arg(&trace)
        .arg("--report")
        .arg(&replay_report)
        .output()
        .unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    let live: Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    let replay: Value = serde_json::from_slice(&fs::read(replay_report).unwrap()).unwrap();
    assert_eq!(live["result"], replay["result"]);
    assert_eq!(replay["provenance"]["mode"], "offline-replay");
    let assertions = directory.path().join("replacement.yaml");
    fs::write(
        &assertions,
        b"assertions: [{before: 30ns, can_seen: {endpoint: host.can, id: 0x777}}]\n",
    )
    .unwrap();
    let replacement_report = directory.path().join("replacement.json");
    let replacement = executable()
        .arg("replay")
        .arg(&trace)
        .arg("--assertions")
        .arg(assertions)
        .arg("--report")
        .arg(&replacement_report)
        .output()
        .unwrap();
    assert_eq!(replacement.status.code(), Some(1));
    let replacement: Value =
        serde_json::from_slice(&fs::read(replacement_report).unwrap()).unwrap();
    assert_eq!(replacement["result"]["status"], serde_json::json!(["fail"]));
    let mut trailing = fs::read(trace).unwrap();
    trailing.extend_from_slice(b"trailing bytes");
    let invalid = directory.path().join("trailing.mcap");
    fs::write(&invalid, trailing).unwrap();
    let invalid_report = directory.path().join("invalid.json");
    let invalid = executable()
        .arg("replay")
        .arg(invalid)
        .arg("--report")
        .arg(&invalid_report)
        .output()
        .unwrap();
    assert_eq!(invalid.status.code(), Some(1));
    let invalid: Value = serde_json::from_slice(&fs::read(invalid_report).unwrap()).unwrap();
    assert_eq!(invalid["status"], "failed");
    assert!(invalid.get("result").is_none());
}

#[cfg(all(target_os = "linux", feature = "host-processes"))]
#[test]
fn host_commands_require_explicit_opt_in_and_finish_cleanup_before_reporting() {
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("processes.yaml");
    fs::write(&source, b"schema: sim/v1\nruntime: {duration: 10ns, quantum: 1ns}\nnodes:\n  programs:\n    kind: app.host-processes\n    commands:\n      worker: {command: [/bin/sleep, '30']}\n").unwrap();
    let validated = executable().arg("validate").arg(&source).output().unwrap();
    assert!(
        validated.status.success(),
        "{}",
        String::from_utf8_lossy(&validated.stderr)
    );
    let denied = executable().arg("run").arg(&source).output().unwrap();
    assert_eq!(denied.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&denied.stderr).contains("--allow-host-processes"));
    let report = directory.path().join("passed.json");
    let passed = executable()
        .arg("run")
        .arg(&source)
        .arg("--allow-host-processes")
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();
    assert!(
        passed.status.success(),
        "{}",
        String::from_utf8_lossy(&passed.stderr)
    );
    let events: Vec<Value> = String::from_utf8(passed.stdout)
        .unwrap()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    let child = events
        .iter()
        .find(|event| event["kind"] == "process.started")
        .unwrap();
    let pid = child["payload"]["pid"].as_u64().unwrap();
    assert!(!PathBuf::from(format!("/proc/{pid}")).exists());
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(report).unwrap()).unwrap()["status"],
        "passed"
    );

    fs::write(&source, b"schema: sim/v1\nruntime: {duration: 1s, quantum: 1ns}\nnodes:\n  programs:\n    kind: app.host-processes\n    commands:\n      worker: {command: [/bin/true]}\n").unwrap();
    let failed = executable()
        .arg("run")
        .arg(source)
        .args(["--allow-host-processes", "--timeout", "2"])
        .output()
        .unwrap();
    assert_eq!(failed.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&failed.stderr).contains("exited"));
}

#[cfg(all(feature = "mujoco", feature = "devices", feature = "trace"))]
#[test]
fn native_joint_scene_records_and_replays_after_the_model_is_removed() {
    let directory = tempfile::tempdir().unwrap();
    let model = directory.path().join("world.xml");
    fs::copy(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/shared-world/world.xml"),
        &model,
    )
    .unwrap();
    let source = directory.path().join("joint.yaml");
    fs::write(&source, b"schema: sim/v1\nruntime: {duration: 3ms, quantum: 1ms}\nnodes:\n  world: {kind: physics.mujoco, model: world.xml, control: bound, joints: {j1: m1, j2: m2}}\n  bus: {kind: bus.can}\n  host: {kind: device.can-host}\n  left: {kind: device.dji-m3508, motor_id: 1, plant: joint, joint: j1}\n  right: {kind: device.dji-m3508, motor_id: 2, plant: joint, joint: j2}\nlinks: [[host.can, bus.can], [left.can, bus.can], [right.can, bus.can], [left.joint, world.drives], [right.joint, world.drives]]\nexperiment:\n  inputs: [{at: 0ns, endpoint: host.tx, value: {id: 0x200, dlc: 8, data_hex: 4000c00000000000}}]\n  assertions:\n    - {before: 3ms, can_seen: {endpoint: left.can, id: 0x201, dlc: 8}}\n    - {before: 3ms, can_seen: {endpoint: right.can, id: 0x202, dlc: 8}}\n").unwrap();
    let trace = directory.path().join("joint.mcap");
    let report = directory.path().join("joint.json");
    let run = executable()
        .arg("run")
        .arg(&source)
        .arg("--trace")
        .arg(&trace)
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();
    assert!(
        run.status.success(),
        "{}",
        String::from_utf8_lossy(&run.stderr)
    );
    let report: Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    assert_eq!(
        report["result"]["status"],
        serde_json::json!(["pass", "pass"])
    );
    fs::remove_file(model).unwrap();
    let replay_report = directory.path().join("replay.json");
    let replay = executable()
        .arg("replay")
        .arg(trace)
        .arg("--report")
        .arg(&replay_report)
        .output()
        .unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    let replay: Value = serde_json::from_slice(&fs::read(replay_report).unwrap()).unwrap();
    assert_eq!(report["result"], replay["result"]);
}
