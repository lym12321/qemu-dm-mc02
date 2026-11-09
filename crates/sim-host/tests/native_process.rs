#![cfg(all(target_os = "linux", feature = "native-process"))]

use rustix::{
    event::{poll, PollFd, PollFlags, Timespec},
    process::{set_child_subreaper, waitpid, Pid, WaitOptions},
};
use sim_host::native_process::{Error, ErrorKind, NativeProcess, ProcessOptions};
use std::{
    ffi::OsString,
    fs,
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    time::{Duration, Instant},
};
use tempfile::{tempdir, TempDir};
use wait_timeout::ChildExt;

struct Fixture {
    directory: TempDir,
}
impl Fixture {
    fn new() -> Self {
        Self {
            directory: tempdir().unwrap(),
        }
    }
    fn record(&self) -> PathBuf {
        self.directory.path().join("child-record")
    }
    fn options(&self, mode: &str) -> ProcessOptions {
        ProcessOptions {
            argv: vec![
                which::which("python3").unwrap().into_os_string(),
                "-u".into(),
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("tests/support/native_process_child.py")
                    .into_os_string(),
                mode.into(),
                self.record().into_os_string(),
                "{qmp}".into(),
            ],
            sockets: vec!["qmp".into()],
            wait_sockets: None,
            startup_timeout: Duration::from_secs(2),
            host_deadline: Instant::now() + Duration::from_secs(4),
        }
    }
    fn recorded(&self) -> (u32, PathBuf, i32, Option<i32>) {
        let content = fs::read_to_string(self.record()).unwrap();
        let mut lines = content.lines();
        (
            lines.next().unwrap().parse().unwrap(),
            lines.next().unwrap().into(),
            lines.next().unwrap().parse().unwrap(),
            lines.next().map(|line| line.parse().unwrap()),
        )
    }
}

fn start(options: ProcessOptions) -> NativeProcess {
    NativeProcess::start(options).unwrap()
}
fn error(options: ProcessOptions) -> Arc<Error> {
    match NativeProcess::start(options) {
        Err(error) => error,
        Ok(mut owner) => {
            owner.close().unwrap();
            panic!("expected start failure")
        }
    }
}
fn gone(pid: u32, directory: &Path) {
    assert!(
        !Path::new("/proc").join(pid.to_string()).exists(),
        "unreaped child {pid}"
    );
    assert!(
        !directory.exists(),
        "unremoved private directory {directory:?}"
    );
}

#[test]
fn actual_socket_is_transport_readiness_and_finish_reaps_and_removes_resources() {
    let fixture = Fixture::new();
    let before = Instant::now();
    let mut owner = start(fixture.options("ready"));
    let (pid, directory, group, _) = fixture.recorded();
    assert_eq!(owner.id(), Some(pid));
    assert_eq!(group as u32, pid);
    assert!(owner.started() >= before && owner.started() <= Instant::now());
    let mut peer = UnixStream::connect(owner.socket("qmp").unwrap()).unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
    let mut marker = [0; 14];
    peer.read_exact(&mut marker).unwrap();
    assert_eq!(&marker, b"transport only");
    owner.check().unwrap();
    drop(peer);
    assert_eq!(owner.finish(Ok(42)).unwrap(), 42);
    gone(pid, &directory);
}

#[test]
fn peer_cleanup_has_its_own_budget_after_the_final_active_check() {
    let fixture = Fixture::new();
    let mut owner = start(fixture.options("ready"));
    let (pid, directory, _, _) = fixture.recorded();
    owner
        .check_with_deadline(Instant::now() + Duration::from_millis(100))
        .unwrap();
    owner.begin_cleanup().unwrap();
    poll(
        &mut [],
        Some(&Timespec::try_from(Duration::from_millis(120)).unwrap()),
    )
    .unwrap();
    owner.close().unwrap();
    gone(pid, &directory);
}

#[test]
fn startup_zero_exit_fails_and_retains_only_the_bounded_stderr_tail() {
    let fixture = Fixture::new();
    let error = error(fixture.options("exit0"));
    assert_eq!(error.kind, ErrorKind::Exited);
    assert!(error.message.contains("status 0"));
    let notes = error.notes();
    let tail = notes
        .iter()
        .find_map(|note| note.strip_prefix("native stderr tail:\n"))
        .unwrap();
    assert_eq!(tail.len(), 12000);
    assert!(!tail.contains("discard-prefix"));
    assert!(tail.ends_with("retained-tail\n"));
    let (pid, directory, _, _) = fixture.recorded();
    gone(pid, &directory);
}

#[test]
fn active_exit_notification_is_borrowed_and_status_zero_is_terminal() {
    let fixture = Fixture::new();
    let mut owner = start(fixture.options("active-exit0"));
    let mut peer = UnixStream::connect(owner.socket("qmp").unwrap()).unwrap();
    let mut marker = [0; 14];
    peer.read_exact(&mut marker).unwrap();
    peer.write_all(b"x").unwrap();
    let notification = owner.exit_fd().unwrap();
    let mut ready = [PollFd::new(&notification, PollFlags::IN)];
    assert_eq!(
        poll(
            &mut ready,
            Some(&Timespec::try_from(Duration::from_secs(2)).unwrap())
        )
        .unwrap(),
        1
    );
    let first = owner.check().unwrap_err();
    assert_eq!(first.kind, ErrorKind::Exited);
    assert!(first.message.contains("status 0"), "{first}");
    assert!(Arc::ptr_eq(&first, &owner.close().unwrap_err()));
    let (pid, directory, _, _) = fixture.recorded();
    gone(pid, &directory);
}

#[test]
fn startup_and_total_deadlines_cover_spawn_and_reject_regular_files() {
    let fixture = Fixture::new();
    let mut options = fixture.options("ready");
    options.host_deadline = Instant::now();
    assert_eq!(error(options).kind, ErrorKind::Deadline);
    assert!(!fixture.record().exists());
    let mut options = fixture.options("regular-file");
    options.startup_timeout = Duration::from_millis(80);
    let error = error(options);
    assert_eq!(error.kind, ErrorKind::Deadline);
    assert!(error.message.contains("socket startup"));
    let (pid, directory, _, _) = fixture.recorded();
    gone(pid, &directory);
    let mut options = fixture.options("ready");
    options.host_deadline = Instant::now() + Duration::from_millis(80);
    let mut owner = start(options);
    let notification = owner.exit_fd().unwrap();
    let mut ready = [PollFd::new(&notification, PollFlags::IN)];
    // No process event: wait until the caller's absolute budget has elapsed.
    assert_eq!(
        poll(
            &mut ready,
            Some(&Timespec::try_from(Duration::from_millis(90)).unwrap())
        )
        .unwrap(),
        0
    );
    let first = owner.check().unwrap_err();
    assert_eq!(first.kind, ErrorKind::Deadline);
    assert!(Arc::ptr_eq(&first, &owner.close().unwrap_err()));
    let (pid, directory, _, _) = fixture.recorded();
    gone(pid, &directory);
}

#[test]
fn term_resistant_child_is_killed_and_consumer_primary_survives_cleanup_errors() {
    let fixture = Fixture::new();
    let mut owner = start(fixture.options("ignore-term"));
    let (pid, directory, _, _) = fixture.recorded();
    fs::remove_file(owner.stderr_path()).unwrap(); // Real read failure after process cleanup.
    let primary = Arc::new(Error::new(ErrorKind::Io, "consumer interrupted"));
    let before = Instant::now();
    let result: sim_host::native_process::Result<()> = owner.finish(Err(primary.clone()));
    assert!(Arc::ptr_eq(&primary, &result.unwrap_err()));
    assert!(
        before.elapsed() >= Duration::from_millis(900) && before.elapsed() < Duration::from_secs(3)
    );
    assert!(primary
        .notes()
        .iter()
        .any(|note| note.contains("native stderr read")));
    gone(pid, &directory);
}

#[test]
fn invalid_inputs_fail_before_child_effects_and_spawn_errors_clean_the_owner() {
    let fixture = Fixture::new();
    for (sockets, waits) in [
        (vec!["bad-name"], None),
        (vec!["qmp", "qmp"], None),
        (vec!["qmp"], Some(vec!["unknown"])),
    ] {
        let mut options = fixture.options("ready");
        options.sockets = sockets.into_iter().map(Into::into).collect();
        options.wait_sockets = waits.map(|names| names.into_iter().map(Into::into).collect());
        assert_eq!(error(options).kind, ErrorKind::Admission);
    }
    let mut options = fixture.options("ready");
    options.argv.push(OsString::from("NUL\0argument"));
    assert_eq!(error(options).kind, ErrorKind::Admission);
    let mut options = fixture.options("ready");
    options.argv[0] = fixture
        .directory
        .path()
        .join("missing executable")
        .into_os_string();
    assert!(error(options).message.contains("native spawn"));
    assert!(!fixture.record().exists());
}

#[test]
fn drop_is_a_bounded_fallback_for_the_same_owner() {
    let fixture = Fixture::new();
    let owner = start(fixture.options("ready"));
    let (pid, directory, _, _) = fixture.recorded();
    drop(owner);
    gone(pid, &directory);
}

#[test]
fn group_stragglers_are_killed_before_the_leader_is_reaped() {
    // Subreaper is process-wide. Isolate this direct consumer in its own test
    // process so adoption never changes the rest of the parallel test suite.
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "group_cleanup_consumer",
            "--ignored",
            "--nocapture",
        ])
        .stdin(Stdio::null())
        .spawn()
        .unwrap();
    match child.wait_timeout(Duration::from_secs(5)).unwrap() {
        Some(status) => assert!(status.success()),
        None => {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("group cleanup consumer timed out");
        }
    }
}

#[test]
#[ignore = "isolated subprocess consumer; run by group_stragglers_are_killed_before_the_leader_is_reaped"]
fn group_cleanup_consumer() {
    set_child_subreaper(Pid::from_raw(1)).unwrap();
    let fixture = Fixture::new();
    let mut owner = start(fixture.options("descendant"));
    let (pid, directory, _, descendant) = fixture.recorded();
    let descendant = Pid::from_raw(descendant.unwrap()).unwrap();
    owner.close().unwrap();
    gone(pid, &directory);
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        if let Some((_, status)) = waitpid(Some(descendant), WaitOptions::NOHANG).unwrap() {
            assert_eq!(status.terminating_signal(), Some(9));
            break;
        }
        assert!(
            Instant::now() < deadline,
            "descendant survived group cleanup"
        );
        std::thread::yield_now();
    }
    assert!(!Path::new("/proc")
        .join(descendant.as_raw_pid().to_string())
        .exists());
}
