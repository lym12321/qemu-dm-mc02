#![cfg(all(target_os = "linux", feature = "host-processes"))]

use sim_core::{EventPayload, NodeSpec, SimulationOptions, SimulationState, Topology};
use sim_host::{
    processes::{HostCommand, HostProcessesOptions},
    runtime::{self, HostEvent, HostOptions, HostSimulation},
};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

fn options(directory: &std::path::Path, allow_launch: bool) -> HostProcessesOptions {
    HostProcessesOptions {
        commands: vec![HostCommand::admit(
            "fixture".into(),
            vec![
                std::env::current_exe()
                    .unwrap()
                    .into_os_string()
                    .into_string()
                    .unwrap(),
                "--exact".into(),
                "host_process_fixture".into(),
                "--ignored".into(),
                "--nocapture".into(),
            ],
            directory.to_owned(),
        )
        .unwrap()],
        allow_launch,
    }
}

fn simulation(options: HostProcessesOptions) -> HostSimulation {
    HostSimulation::new(
        Topology::new(
            vec![NodeSpec::new(
                "programs",
                "app.host-processes",
                HostOptions::HostProcesses(options),
            )],
            vec![],
        )
        .unwrap(),
        runtime::registry().unwrap(),
        SimulationOptions {
            quantum_ns: 1,
            host_timeout: Some(Duration::from_secs(5)),
            ..Default::default()
        },
    )
    .unwrap()
}

#[test]
fn command_admission_retains_launch_spelling_and_rejects_resource_limits() {
    use std::os::unix::fs::symlink;
    let directory = tempfile::tempdir().unwrap();
    let alias = directory.path().join("command");
    symlink("/bin/cat", &alias).unwrap();
    let command = HostCommand::admit(
        "read".into(),
        vec!["./command".into(), "$(literal)".into()],
        directory.path().to_owned(),
    )
    .unwrap();
    assert_eq!(command.argv(), &[alias.to_str().unwrap(), "$(literal)"]);
    for argv in [
        vec![],
        vec!["/bin/cat\0".into()],
        vec!["/bin/cat".into(), "a".repeat(4097)],
        vec!["/bin/cat".into(); 129],
        vec![
            "/bin/cat".into(),
            "a".repeat(4096),
            "b".repeat(4096),
            "c".repeat(4096),
            "d".repeat(4096),
        ],
    ] {
        assert!(HostCommand::admit("read".into(), argv, directory.path().to_owned()).is_err());
    }
    let mut denied = simulation(options(directory.path(), false));
    assert!(denied
        .start(None)
        .unwrap_err()
        .to_string()
        .contains("--allow-host-processes"));
    denied.close().unwrap();
    assert!(!directory.path().join("ready").exists());
}

#[test]
fn sampled_pipes_are_bounded_exit_zero_freezes_and_cleanup_reaps() {
    let directory = tempfile::tempdir().unwrap();
    let mut simulation = simulation(options(directory.path(), true));
    simulation.start(None).unwrap();
    let pid = simulation
        .events()
        .iter()
        .find_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::ProcessStarted { pid, .. }) => Some(pid),
            _ => None,
        })
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let ready = directory.path().join("ready");
    let mut seen = false;
    while !ready.exists() || !seen {
        assert!(Instant::now() < deadline);
        simulation
            .run_until(simulation.t_sim_ns() + 1, None)
            .unwrap();
        for event in simulation.events() {
            if let EventPayload::Observation(HostEvent::ProcessOutput { stdout, stderr, .. }) =
                event.payload
            {
                assert!(stdout.len() <= 12_000 && stderr.len() <= 12_000);
                seen |= stdout.contains("STDOUT_END") && stderr.contains("STDERR_END");
            }
        }
        std::thread::yield_now();
    }
    fs::write(directory.path().join("exit"), []).unwrap();
    let failure = loop {
        match simulation.run_until(simulation.t_sim_ns() + 1, None) {
            Err(error) => break error,
            Ok(_) => {
                assert!(Instant::now() < deadline);
                std::thread::yield_now();
            }
        }
    };
    assert!(failure.to_string().contains("fixture exited (0)"));
    assert_eq!(simulation.state(), SimulationState::Failed);
    assert!(Arc::ptr_eq(&failure, &simulation.failure().unwrap()));
    simulation.close().unwrap();
    assert!(!PathBuf::from(format!("/proc/{pid}")).exists());
}

#[test]
#[ignore = "subprocess fixture invoked by the tests above"]
fn host_process_fixture() {
    let mut stdout = std::io::stdout().lock();
    let mut stderr = std::io::stderr().lock();
    stdout.write_all(&vec![b'o'; 20_000]).unwrap();
    stdout.write_all(b"STDOUT_END").unwrap();
    stdout.flush().unwrap();
    stderr.write_all(&vec![b'e'; 20_000]).unwrap();
    stderr.write_all(b"STDERR_END").unwrap();
    stderr.flush().unwrap();
    fs::write("ready", []).unwrap();
    while !std::path::Path::new("exit").exists() {
        std::thread::park_timeout(Duration::from_millis(1));
    }
}
