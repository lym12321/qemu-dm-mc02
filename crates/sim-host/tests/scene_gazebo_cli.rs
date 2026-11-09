#![cfg(all(feature = "scene", feature = "gazebo", feature = "trace"))]

use serde_json::Value;
use std::{fs, path::Path, process::Command};

fn cli(command: &str, path: &Path, report: &Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_sim"))
        .arg(command)
        .arg(path)
        .arg("--report")
        .arg(report)
        .output()
        .unwrap()
}
fn report(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

#[test]
#[ignore = "requires a separate scene+trace replay executable"]
fn actual_gazebo_scene_cli_records_completed_si_then_replays_without_model_or_sdk() {
    let temporary = tempfile::tempdir().unwrap();
    let directory = match std::env::var_os("SIM_GAZEBO_ARTIFACT_DIR") {
        Some(path) => {
            let path = std::path::PathBuf::from(path);
            fs::create_dir(&path).unwrap();
            path
        }
        None => temporary.path().to_owned(),
    };
    let model = directory.join("rotor.sdf");
    fs::write(
        &model,
        include_bytes!("../../../models/gazebo_imu_rotor.sdf"),
    )
    .unwrap();
    let source = directory.join("scene.yaml");
    fs::write(&source, b"schema: sim/v1\nruntime: {duration: 10ms, quantum: 2ms}\nnodes:\n  world:\n    kind: physics.gazebo\n    model: rotor.sdf\n    resource_paths: [.]\n    joints: {shaft: [rotor, shaft]}\n    bodies: {body: [rotor, body]}\n    imus: {imu: [rotor, body, imu]}\nexperiment:\n  inputs: [{at: 0ns, endpoint: world.effort, value: {shaft: 0.4}}]\n").unwrap();
    let validated = Command::new(env!("CARGO_BIN_EXE_sim"))
        .arg("validate")
        .arg(&source)
        .output()
        .unwrap();
    assert!(
        validated.status.success(),
        "{}",
        String::from_utf8_lossy(&validated.stderr)
    );
    let trace = directory.join("events.mcap");
    let live_path = directory.join("live.json");
    let live = Command::new(env!("CARGO_BIN_EXE_sim"))
        .arg("run")
        .arg(&source)
        .arg("--report")
        .arg(&live_path)
        .arg("--trace")
        .arg(&trace)
        .output()
        .unwrap();
    fs::write(directory.join("live.stdout"), &live.stdout).unwrap();
    fs::write(directory.join("live.stderr"), &live.stderr).unwrap();
    assert!(
        live.status.success(),
        "{}",
        String::from_utf8_lossy(&live.stderr)
    );
    let events = String::from_utf8(live.stdout)
        .unwrap()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .collect::<Vec<_>>();
    let states = events
        .iter()
        .filter(|event| event["kind"] == "physics.state")
        .collect::<Vec<_>>();
    assert_eq!(states.len(), 6);
    let target = &states[5]["payload"];
    assert_eq!(target["native_time_s"], 0.01);
    let velocity = target["joints"]["shaft"]["velocity_rad_s"]
        .as_f64()
        .unwrap();
    assert!((velocity - 0.2).abs() < 1e-10, "{velocity}");
    assert_eq!(
        target["imus"]["imu"]["accel_m_s2"],
        serde_json::json!([0.0, 0.0, 9.80665])
    );
    let live = report(&live_path);
    assert_eq!(live["status"], "passed");
    assert_eq!(live["result"]["end_ns"], 10_000_000);
    assert_eq!(live["result"]["delivered_inputs"], 1);
    fs::remove_file(model).unwrap();
    fs::remove_file(source).unwrap();
    let replay_path = directory.join("replay.json");
    let replay = Command::new(
        std::env::var_os("SIM_OFFLINE_REPLAY_EXE").expect("separate scene+trace binary"),
    )
    .arg("replay")
    .arg(trace)
    .arg("--report")
    .arg(&replay_path)
    .env("LD_LIBRARY_PATH", "")
    .env("QEMU_SYSTEM_ARM", "/no/native/backend")
    .output()
    .unwrap();
    fs::write(directory.join("replay.stdout"), &replay.stdout).unwrap();
    fs::write(directory.join("replay.stderr"), &replay.stderr).unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    let replay = report(&replay_path);
    assert_eq!(live["result"], replay["result"]);
}

#[test]
fn actual_gazebo_cli_off_grid_target_reports_failure_at_the_last_commit() {
    let directory = tempfile::tempdir().unwrap();
    fs::write(
        directory.path().join("rotor.sdf"),
        include_bytes!("../../../models/gazebo_imu_rotor.sdf"),
    )
    .unwrap();
    let source = directory.path().join("scene.yaml");
    fs::write(&source, b"schema: sim/v1\nruntime: {duration: 1ns, quantum: 1ns}\nnodes:\n  world: {kind: physics.gazebo, model: rotor.sdf, imus: {imu: [rotor, body, imu]}}\n").unwrap();
    let path = directory.path().join("failed.json");
    let output = cli("run", &source, &path);
    assert_eq!(output.status.code(), Some(1));
    let failed = report(&path);
    assert_eq!(failed["status"], "failed");
    assert_eq!(failed["time_ns"], 0);
    assert!(
        failed["error"].as_str().unwrap().contains("step"),
        "{failed}"
    );
}
