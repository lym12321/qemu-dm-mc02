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
    let deadline = Instant::now() + Duration::from_secs(6);
    loop {
        let data: Vec<_> = session
            .history(0)
            .iter()
            .filter(|event| event["type"] == "uart" && event["endpoint"] == "uart1")
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
    assert!(!PathBuf::from(format!("/proc/{}", identity.0)).exists());
    assert!(!identity.1.exists());
    assert!(!qmp.exists());
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
fn removed_selected_firmware_failure_remains_visible_through_repeated_close() {
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
    wait_state(&session, |state| state["status"] == "failed");
    let closed = session.close().unwrap_err();
    assert!(std::sync::Arc::ptr_eq(&failure.primary, &closed.primary));
    let repeated = session.close().unwrap_err();
    assert!(std::sync::Arc::ptr_eq(&failure.primary, &repeated.primary));
}
