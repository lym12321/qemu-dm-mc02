#![cfg(all(target_os = "linux", feature = "lab-session"))]
use base64::{engine::general_purpose::STANDARD, Engine as _};
use futures::FutureExt;
use serde_json::{json, Value};
use sim_host::lab::session::{CommandReply, LabSession, SessionError, SessionOptions, StopReply};
use std::{
    fs,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

fn reply(mut reply: CommandReply) -> Result<Value, SessionError> {
    let deadline = Instant::now() + Duration::from_secs(6);
    loop {
        if let Some(value) = reply.try_recv().expect("owner dropped command reply") {
            return value;
        }
        assert!(Instant::now() < deadline, "command reply timed out");
        thread::sleep(Duration::from_millis(1));
    }
}
fn stopped(reply: StopReply) -> Result<Value, SessionError> {
    let deadline = Instant::now() + Duration::from_secs(6);
    loop {
        if let Some(value) = reply.clone().now_or_never() {
            return value.expect("owner dropped stop reply");
        }
        assert!(Instant::now() < deadline, "stop reply timed out");
        thread::sleep(Duration::from_millis(1));
    }
}
fn wait_state(session: &LabSession, condition: impl Fn(&Value) -> bool) -> Value {
    let deadline = Instant::now() + Duration::from_secs(6);
    loop {
        let state = session.state();
        if condition(&state) {
            return state;
        }
        assert!(Instant::now() < deadline, "last owner state {state}");
        thread::sleep(Duration::from_millis(2));
    }
}
fn wait_uart(session: &LabSession, wanted: &[u8]) {
    wait_uart_for(session, wanted, None);
}
fn wait_uart_for(session: &LabSession, wanted: &[u8], generation: Option<&str>) {
    let deadline = Instant::now() + Duration::from_secs(6);
    loop {
        let data: Vec<_> = session
            .history(0)
            .iter()
            .filter(|event| event["type"] == "uart" && event["endpoint"] == "uart1")
            .filter(|event| generation.is_none_or(|id| event["session_id"] == id))
            .flat_map(|event| {
                STANDARD
                    .decode(event["data_b64"].as_str().unwrap())
                    .unwrap()
            })
            .collect();
        if data.windows(wanted.len()).any(|window| window == wanted) {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "missing UART marker: {}",
            String::from_utf8_lossy(&data)
        );
        thread::sleep(Duration::from_millis(2));
    }
}
fn native_options(qmp: &Path, firmware: &str) -> SessionOptions {
    SessionOptions {
        firmware: Some(std::env::var_os(firmware).expect(firmware).into()),
        qemu: Some(
            std::env::var_os("SIM_NATIVE_QEMU")
                .expect("SIM_NATIVE_QEMU")
                .into(),
        ),
        startup_timeout: Duration::from_secs(3),
        timeout: Duration::from_secs(10),
        qmp_path: Some(qmp.to_owned()),
        gdb_port: None,
        usbip: None,
        ..SessionOptions::default()
    }
}
fn identity(qmp: &Path) -> (u32, PathBuf) {
    for entry in fs::read_dir("/proc").unwrap().flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<u32>() else {
            continue;
        };
        let Ok(bytes) = fs::read(entry.path().join("cmdline")) else {
            continue;
        };
        let args: Vec<_> = bytes
            .split(|byte| *byte == 0)
            .map(|bytes| String::from_utf8_lossy(bytes))
            .collect();
        if args
            .iter()
            .any(|arg| arg.contains(&qmp.display().to_string()))
        {
            let internal = args.windows(2).find(|pair| pair[0] == "-qmp").unwrap()[1]
                .strip_prefix("unix:")
                .unwrap()
                .split(',')
                .next()
                .unwrap();
            return (pid, PathBuf::from(internal).parent().unwrap().to_owned());
        }
    }
    panic!("native process with selected public QMP socket not found")
}
fn cleaned(identity: (u32, PathBuf), qmp: &Path) {
    process_cleaned(identity);
    assert!(!qmp.exists());
}
fn process_cleaned(identity: (u32, PathBuf)) {
    assert!(!PathBuf::from(format!("/proc/{}", identity.0)).exists());
    assert!(!identity.1.exists());
}
fn loaded_image(pid: u32) -> PathBuf {
    let bytes = fs::read(format!("/proc/{pid}/cmdline")).unwrap();
    let args: Vec<_> = bytes.split(|byte| *byte == 0).collect();
    let image = args.windows(2).find(|pair| pair[0] == b"-kernel").unwrap()[1];
    PathBuf::from(String::from_utf8(image.to_vec()).unwrap())
}

#[test]
fn idle_selection_and_stop_do_not_launch_native_processes() {
    let options = SessionOptions {
        qemu: Some(std::env::current_exe().unwrap()),
        ..SessionOptions::default()
    };
    let session = LabSession::new(options).unwrap();
    assert_eq!(session.state()["status"], "idle");
    let error = reply(session.submit("load", json!({}), None, None)).unwrap_err();
    assert!(error.input && error.to_string().contains("select firmware"));
    assert!(
        reply(session.submit("run", json!({}), Some("stale"), None))
            .unwrap_err()
            .input
    );
    stopped(session.request_stop(None)).unwrap();
    session.close().unwrap();
    session.close().unwrap();
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ELF"]
fn stop_cancels_native_startup_and_same_owner_can_start_again() {
    use std::os::unix::fs::PermissionsExt;
    let directory = tempfile::tempdir().unwrap();
    let record = directory.path().join("startup.json");
    let gate = directory.path().join("allow-start");
    let wrapper = directory.path().join("qemu-wrapper");
    let qemu = PathBuf::from(std::env::var_os("SIM_NATIVE_QEMU").unwrap());
    fs::write(&wrapper, format!(
        "#!{}\nimport json,os,sys,time\nqmp=sys.argv[sys.argv.index('-qmp')+1].split(',')[0][5:]\nwith open({},'w') as f: json.dump([os.getpid(),os.path.dirname(qmp)],f)\nwhile not os.path.exists({}): time.sleep(.005)\nos.execv({},[{},*sys.argv[1:]])\n",
        which::which("python3").unwrap().display(), json!(record), json!(gate), json!(qemu), json!(qemu),
    )).unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
    let public_qmp = directory.path().join("public.qmp");
    let mut options = native_options(&public_qmp, "SIM_NATIVE_ELF");
    options.qemu = Some(wrapper);
    options.startup_timeout = Duration::from_secs(10);
    options.timeout = Duration::from_secs(20);
    let session = LabSession::new(options).unwrap();
    let starting = session.submit("start", json!({}), None, Some("page"));
    let deadline = Instant::now() + Duration::from_secs(3);
    while fs::read(&record)
        .ok()
        .is_none_or(|bytes| serde_json::from_slice::<Value>(&bytes).is_err())
    {
        assert!(Instant::now() < deadline, "wrapper did not start");
        thread::sleep(Duration::from_millis(2));
    }
    let original: (u32, PathBuf) = serde_json::from_slice(&fs::read(&record).unwrap()).unwrap();
    let generation = session.state()["session_id"].as_str().unwrap().to_owned();
    let before = Instant::now();
    stopped(session.request_stop(Some(&generation))).unwrap();
    assert!(
        before.elapsed() < Duration::from_secs(2),
        "stop waited for startup deadline"
    );
    assert!(reply(starting)
        .unwrap_err()
        .to_string()
        .contains("cancelled"));
    assert_eq!(session.state()["status"], "stopped");
    assert!(session.state()["error"].is_null());
    cleaned(original, &public_qmp);

    fs::write(gate, b"start").unwrap();
    reply(session.submit("start", json!({}), Some(&generation), Some("page"))).unwrap();
    wait_uart(&session, b"SIM_READY minimal-board\n");
    let replacement = identity(&public_qmp);
    stopped(session.request_stop(None)).unwrap();
    cleaned(replacement, &public_qmp);
    session.close().unwrap();
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ELF"]
fn actual_native_owner_projects_events_releases_key_and_restarts_only_after_cleanup() {
    let dir = tempfile::tempdir().unwrap();
    let qmp = dir.path().join("public.qmp");
    let session = LabSession::new(native_options(&qmp, "SIM_NATIVE_ELF")).unwrap();
    reply(session.submit("load", json!({}), None, Some("page"))).unwrap();
    let state = wait_state(&session, |s| s["status"] == "paused");
    let generation = state["session_id"].as_str().unwrap().to_owned();
    let first = identity(&qmp);
    assert_eq!(state["board"]["timestamp_ns"], "0");
    assert_eq!(state["pins"].as_array().unwrap().len(), 128);
    reply(session.submit("run", json!({}), Some(&generation), Some("page"))).unwrap();
    wait_uart(&session, b"SIM_READY minimal-board\n");
    reply(session.submit(
        "key",
        json!({"pressed":true}),
        Some(&generation),
        Some("page"),
    ))
    .unwrap();
    wait_uart(&session, b"KEY pressed B0=1\n");
    session.disconnect("page");
    wait_state(&session, |state| state["board"]["key"]["pressed"] == false);
    assert_eq!(session.state()["session_id"], generation);
    reply(session.submit("pause", json!({}), Some(&generation), Some("new-page"))).unwrap();
    let paused = wait_state(&session, |state| state["status"] == "paused");
    let error = reply(session.submit(
        "adc",
        json!({"channel":4,"raw":1}),
        Some(&generation),
        Some("new-page"),
    ))
    .unwrap_err();
    assert!(error.input && error.to_string().contains("running"));
    assert_eq!(session.state()["status"], "paused");
    assert!(paused["remaining_seconds"].as_f64().unwrap() > 0.0);
    let last_sequence = session.history(0).last().unwrap()["seq"]
        .as_str()
        .unwrap()
        .parse::<u64>()
        .unwrap();
    // Stop survives dropped HTTP waiters and only completes after native cleanup.
    drop(session.request_stop(Some(&generation)));
    stopped(session.request_stop(Some(&generation))).unwrap();
    cleaned(first, &qmp);
    assert_eq!(session.state()["status"], "stopped");
    assert_eq!(session.state()["observation_stale"], true);
    reply(session.submit("load", json!({}), Some(&generation), Some("new-page"))).unwrap();
    let replacement = wait_state(&session, |state| state["status"] == "paused");
    let next_generation = replacement["session_id"].as_str().unwrap();
    assert_ne!(next_generation, generation);
    let second = identity(&qmp);
    assert!(
        reply(session.submit("run", json!({}), Some(&generation), Some("page")))
            .unwrap_err()
            .input
    );
    reply(session.submit("run", json!({}), Some(next_generation), Some("new-page"))).unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while session.history(last_sequence).is_empty() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(2));
    }
    stopped(session.request_stop(Some(next_generation))).unwrap();
    cleaned(second, &qmp);
    session.close().unwrap();
}

#[test]
fn removed_selected_firmware_is_rejected_before_launch() {
    let dir = tempfile::tempdir().unwrap();
    let bad = dir.path().join("broken.elf");
    fs::write(&bad, b"broken native loader input").unwrap();
    let options = SessionOptions {
        firmware: Some(bad.clone()),
        qemu: Some(std::env::current_exe().unwrap()),
        ..SessionOptions::default()
    };
    let session = LabSession::new(options).unwrap();
    fs::remove_file(bad).unwrap();
    let failure = reply(session.submit("load", json!({}), None, None)).unwrap_err();
    assert!(failure.input);
    assert_eq!(session.state()["status"], "idle");
    session.close().unwrap();
    session.close().unwrap();
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU, SIM_NATIVE_ELF and SIM_NATIVE_ADC_ELF"]
fn project_save_keeps_active_launch_and_reload_reads_new_elf() {
    use sim_host::lab::project::{LabProject, ProjectFile};
    use std::sync::Arc;
    let directory = tempfile::tempdir().unwrap();
    let elf = directory.path().join("firmware.elf");
    fs::copy(std::env::var_os("SIM_NATIVE_ELF").unwrap(), &elf).unwrap();
    let file = Arc::new(ProjectFile::open(directory.path().join("sim.lab.json")).unwrap());
    let mut options = native_options(&directory.path().join("public.qmp"), "SIM_NATIVE_ELF");
    options.project = Some(file.clone());
    let session = LabSession::new(options).unwrap();
    assert!(!file.path().exists());
    let mut config = LabProject {
        firmware: Some("firmware.elf".into()),
        startup_timeout_secs: 3.0,
        timeout_secs: 10.0,
        ..LabProject::default()
    };
    let result =
        reply(session.submit("start", json!({"config": config}), None, Some("page"))).unwrap();
    let generation = result["session_id"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| session.state()["session_id"].as_str().unwrap().to_owned());
    wait_uart(&session, b"SIM_READY minimal-board\n");
    config.timeout_secs = 7.0;
    config.actions.insert(
        "vin".into(),
        sim_host::lab::project::Shortcut::Vin { millivolts: 18000 },
    );
    session.save_project(json!(config)).unwrap();
    assert!(file.path().exists());
    assert_eq!(session.state()["config"]["timeout_secs"], 10.0);
    assert!(
        reply(session.submit(
            "action",
            json!({"name":"vin"}),
            Some(&generation),
            Some("page")
        ))
        .unwrap_err()
        .input
    );
    stopped(session.request_stop(Some(&generation))).unwrap();
    fs::copy(std::env::var_os("SIM_NATIVE_ADC_ELF").unwrap(), &elf).unwrap();
    let last = session.history(0).last().unwrap()["seq"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    reply(session.submit(
        "start",
        json!({"config":config}),
        Some(&generation),
        Some("page"),
    ))
    .unwrap();
    let state = wait_state(&session, |state| state["status"] == "running");
    let next = state["session_id"].as_str().unwrap();
    assert_ne!(next, generation);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let data: Vec<_> = session
            .history(last)
            .iter()
            .filter(|event| event["type"] == "uart")
            .flat_map(|event| {
                STANDARD
                    .decode(event["data_b64"].as_str().unwrap())
                    .unwrap()
            })
            .collect();
        if data
            .windows(b"LAB_READY\n".len())
            .any(|bytes| bytes == b"LAB_READY\n")
        {
            break;
        }
        assert!(Instant::now() < deadline, "new ELF marker missing");
        thread::sleep(Duration::from_millis(2));
    }
    reply(session.submit("action", json!({"name":"vin"}), Some(next), Some("page"))).unwrap();
    wait_state(&session, |state| state["board"]["vin_mv"] == 18000);
    stopped(session.request_stop(Some(next))).unwrap();
    session.close().unwrap();
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU, SIM_NATIVE_ELF and SIM_NATIVE_ADC_ELF"]
fn watched_elf_replaces_native_only_after_valid_content_and_preserves_pause_and_stop() {
    use sim_host::lab::project::LabProject;
    let directory = tempfile::tempdir().unwrap();
    let elf = directory.path().join("firmware.elf");
    let original = fs::read(std::env::var_os("SIM_NATIVE_ELF").unwrap()).unwrap();
    let replacement = fs::read(std::env::var_os("SIM_NATIVE_ADC_ELF").unwrap()).unwrap();
    fs::write(&elf, &original).unwrap();
    let qmp = directory.path().join("public.qmp");
    let mut options = native_options(&qmp, "SIM_NATIVE_ELF");
    options.launch = Some(LabProject {
        firmware: Some(elf.clone()),
        watch_elf: true,
        startup_timeout_secs: 3.0,
        timeout_secs: 30.0,
        ..LabProject::default()
    });
    let session = LabSession::new(options).unwrap();
    reply(session.submit("start", json!({}), None, Some("page"))).unwrap();
    let first = session.state()["session_id"].as_str().unwrap().to_owned();
    wait_uart_for(&session, b"SIM_READY minimal-board\n", Some(&first));
    let first_process = identity(&qmp);
    let first_image = loaded_image(first_process.0);
    assert_ne!(first_image, elf);
    assert_eq!(fs::read(&first_image).unwrap(), original);

    // A normal linker may remove or partially rewrite its output. Neither is
    // a reason to tear down the healthy native instance.
    for incomplete in [None, Some(b"\x7fELF".as_slice())] {
        match incomplete {
            None => fs::remove_file(&elf).unwrap(),
            Some(bytes) => fs::write(&elf, bytes).unwrap(),
        }
        let expected = if incomplete.is_none() {
            "cannot read"
        } else {
            "incomplete or invalid ELF"
        };
        let state = wait_state(&session, |state| {
            state["reload_error"]
                .as_str()
                .is_some_and(|error| error.contains(expected))
        });
        assert_eq!(state["status"], "running");
        assert_eq!(state["session_id"], first);
        assert_eq!(identity(&qmp), first_process);
    }
    let staged = directory.path().join("linked.elf");
    fs::write(&staged, &replacement).unwrap();
    fs::rename(staged, &elf).unwrap();
    let state = wait_state(&session, |state| {
        state["session_id"] != first && state["status"] == "running"
    });
    let second = state["session_id"].as_str().unwrap().to_owned();
    wait_uart_for(&session, b"LAB_READY\n", Some(&second));
    process_cleaned(first_process);
    assert!(!first_image.exists(), "previous immutable image leaked");
    assert_eq!(state["config"]["firmware"], json!(elf));
    let second_process = identity(&qmp);
    let second_image = loaded_image(second_process.0);

    // Rebuilding identical bytes does not reset the guest. Observe for longer
    // than a full candidate-settle interval, then change a paused guest.
    fs::write(&elf, &replacement).unwrap();
    let until = Instant::now() + Duration::from_millis(1100);
    while Instant::now() < until {
        assert_eq!(session.state()["session_id"], second);
        thread::sleep(Duration::from_millis(10));
    }
    reply(session.submit("pause", json!({}), Some(&second), Some("page"))).unwrap();
    wait_state(&session, |state| state["status"] == "paused");
    fs::write(&elf, &original).unwrap();
    let state = wait_state(&session, |state| {
        state["session_id"] != second && state["status"] == "paused"
    });
    let third = state["session_id"].as_str().unwrap().to_owned();
    assert_eq!(state["board"]["timestamp_ns"], "0");
    process_cleaned(second_process);
    assert!(!second_image.exists());
    reply(session.submit("run", json!({}), Some(&third), Some("page"))).unwrap();
    wait_uart_for(&session, b"SIM_READY minimal-board\n", Some(&third));
    let third_process = identity(&qmp);
    let third_image = loaded_image(third_process.0);

    fs::write(&elf, &replacement).unwrap();
    stopped(session.request_stop(None)).unwrap();
    cleaned(third_process, &qmp);
    assert!(!third_image.exists());
    let until = Instant::now() + Duration::from_millis(1100);
    while Instant::now() < until {
        let state = session.state();
        assert_eq!(state["status"], "stopped");
        assert_eq!(state["session_id"], third);
        thread::sleep(Duration::from_millis(10));
    }
    session.close().unwrap();
}
