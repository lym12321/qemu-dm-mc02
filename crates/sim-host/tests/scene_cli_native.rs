#![cfg(all(
    target_os = "linux",
    feature = "scene",
    feature = "trace",
    feature = "machine"
))]

use serde_json::Value;
use std::{
    fs,
    io::{BufRead, BufReader, Read},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};
use wait_timeout::ChildExt;

fn asset(name: &str) -> PathBuf {
    fs::canonicalize(std::env::var_os(name).expect(name)).unwrap()
}

fn manifest(path: &Path, duration: &str) {
    let firmware = serde_json::to_string(&asset("SIM_NATIVE_ELF")).unwrap();
    fs::write(path, format!(
        "schema: sim/v1\nruntime: {{duration: {duration}, quantum: 1ms}}\nnodes:\n  main: {{kind: machine.qemu, firmware: {firmware}}}\nexperiment:\n  ready: [{{name: boot, endpoint: main.uart1, text: \"SIM_READY minimal-board\\n\"}}]\n  inputs: [{{at: 1ms, endpoint: main.key, value: true}}]\n  assertions:\n    - {{before: 15ms, uart_contains: {{endpoint: main.uart1, text: \"KEY pressed B0=1\\n\"}}}}\n    - {{before: 15ms, digital_equals: {{endpoint: main.gpio.B0, value: 1}}}}\n"
    )).unwrap();
}

fn run(path: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_sim"));
    command
        .args(["run"])
        .arg(path)
        .arg("--qemu")
        .arg(asset("SIM_NATIVE_QEMU"));
    command.args(["--timeout", "30"]);
    command
}

#[test]
#[ignore = "requires project QEMU and retained minimal-board ELF"]
fn native_cli_ready_input_report_and_replay() {
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("scene.yaml");
    let report = directory.path().join("live.json");
    let replay_report = directory.path().join("replay.json");
    let trace = directory.path().join("events.mcap");
    let qmp = directory.path().join("user.qmp");
    manifest(&source, "20ms");
    let output = run(&source)
        .arg("--report")
        .arg(&report)
        .arg("--trace")
        .arg(&trace)
        .arg("--qmp")
        .arg(&qmp)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!qmp.exists(), "native QMP path survived successful cleanup");
    let live: Value = serde_json::from_slice(&fs::read(&report).unwrap()).unwrap();
    assert_eq!(live["status"], "passed");
    assert_eq!(
        live["result"]["status"],
        serde_json::json!(["pass", "pass"])
    );
    assert_eq!(live["result"]["delivered_inputs"], 1);
    assert_eq!(
        live["result"]["ready_receipts"]["boot"]["quality"],
        "upper_bound"
    );
    assert_eq!(live["result"]["boards"]["main"]["native_origin_ns"], 0);
    let output = Command::new(env!("CARGO_BIN_EXE_sim"))
        .arg("replay")
        .arg(&trace)
        .arg("--report")
        .arg(&replay_report)
        .env("QEMU_SYSTEM_ARM", "/no/native/backend")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let replay: Value = serde_json::from_slice(&fs::read(replay_report).unwrap()).unwrap();
    assert_eq!(live["result"], replay["result"]);
}

#[cfg(feature = "mujoco")]
#[test]
#[ignore = "requires native QEMU, ADC/IMU ELF and a separate scene+trace replay executable"]
fn native_physics_imu_recording_replays_without_native_features_or_inputs() {
    let temporary = tempfile::tempdir().unwrap();
    let directory = match std::env::var_os("SIM_NATIVE_ARTIFACT_DIR") {
        Some(path) => {
            let path = PathBuf::from(path);
            fs::create_dir(&path).unwrap();
            path
        }
        None => temporary.path().to_owned(),
    };
    let source = directory.join("scene.yaml");
    let model = directory.join("world.xml");
    fs::copy(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/shared-world/imu.xml"),
        &model,
    )
    .unwrap();
    let manifest = include_str!("../../../examples/adc-imu-readback/scene.yaml")
        .replace(
            "./build/firmware.elf",
            &serde_json::to_string(&asset("SIM_NATIVE_ADC_IMU_ELF")).unwrap(),
        )
        .replace(
            "../shared-world/imu.xml",
            &serde_json::to_string(&fs::canonicalize(&model).unwrap()).unwrap(),
        );
    fs::write(&source, manifest).unwrap();
    let report = directory.join("live.json");
    let trace = directory.join("events.mcap");
    let output = run(&source)
        .arg("--trace")
        .arg(&trace)
        .arg("--report")
        .arg(&report)
        .output()
        .unwrap();
    fs::write(directory.join("live.stdout.jsonl"), &output.stdout).unwrap();
    fs::write(directory.join("live.stderr"), &output.stderr).unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let live: Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    assert_eq!(live["status"], "passed");
    assert_eq!(
        live["result"]["status"],
        serde_json::json!(["pass", "pass"])
    );
    assert_eq!(live["result"]["delivered_inputs"], 2);
    let events = String::from_utf8(output.stdout).unwrap();
    assert!(events
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .any(|event| event["kind"] == "imu-consumption"
            && event["payload"]["status"] == 0
            && event["payload"]["consumed_mask"] == 3
            && event["payload"]["missing_mask"] == 0));
    fs::remove_file(model).unwrap();
    fs::remove_file(source).unwrap();
    let replay_report = directory.join("replay.json");
    let output = Command::new(asset("SIM_OFFLINE_REPLAY_EXE"))
        .arg("replay")
        .arg(&trace)
        .arg("--report")
        .arg(&replay_report)
        .env("QEMU_SYSTEM_ARM", "/no/native/backend")
        .env("LD_LIBRARY_PATH", "")
        .output()
        .unwrap();
    fs::write(directory.join("replay.stdout"), &output.stdout).unwrap();
    fs::write(directory.join("replay.stderr"), &output.stderr).unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let replay: Value = serde_json::from_slice(&fs::read(replay_report).unwrap()).unwrap();
    assert_eq!(live["result"], replay["result"]);
}

#[test]
#[ignore = "requires project QEMU and retained minimal-board ELF"]
fn native_cli_sigterm_reaps_actual_child_before_failed_report() {
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("long.yaml");
    let report = directory.path().join("interrupted.json");
    manifest(&source, "3600s");
    let mut child = run(&source)
        .arg("--report")
        .arg(&report)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut first = String::new();
    stdout.read_line(&mut first).unwrap();
    let children = fs::read_to_string(format!("/proc/{0}/task/{0}/children", child.id())).unwrap();
    let pids: Vec<u32> = children
        .split_whitespace()
        .map(|pid| pid.parse().unwrap())
        .collect();
    let mut paths = Vec::new();
    for pid in &pids {
        let command = fs::read(format!("/proc/{pid}/cmdline")).unwrap();
        for argument in command.split(|byte| *byte == 0) {
            if let Ok(argument) = std::str::from_utf8(argument) {
                if let Some(path) = argument.strip_prefix("unix:/tmp/dm-qemu.native.") {
                    let directory = path.split('/').next().unwrap();
                    paths.push(PathBuf::from(format!("/tmp/dm-qemu.native.{directory}")));
                }
            }
        }
    }
    let killed = Command::new("kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .unwrap();
    let status = child
        .wait_timeout(Duration::from_secs(5))
        .unwrap()
        .unwrap_or_else(|| {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("native CLI failed to unwind SIGTERM");
        });
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    assert!(killed.success());
    assert!(
        !pids.is_empty(),
        "no live native child at first UART: {first}; {stderr}"
    );
    assert_eq!(status.code(), Some(130), "{stderr}");
    for pid in pids {
        assert!(!PathBuf::from(format!("/proc/{pid}")).exists());
    }
    for path in paths {
        assert!(!path.exists());
    }
    let value: Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
    assert_eq!(value["status"], "failed");
    assert_eq!(value["error"], "simulation interrupted");
}
