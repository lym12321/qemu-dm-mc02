#![cfg(all(windows, feature = "native-process"))]
//! Real Windows processes and local streams; these do not attest ARM execution.

use serde_json::Value;
use sim_host::native_process::{Error, ErrorKind, NativeProcess, ProcessOptions};
use std::{
    ffi::OsString,
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tempfile::TempDir;
use windows_sys::Win32::{
    Foundation::WAIT_OBJECT_0,
    System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE},
};

const LITERAL: &str = "literal \"quotes\" and \\ 路径";
struct Fixture(TempDir);
impl Fixture {
    fn new() -> Self {
        Self(
            tempfile::Builder::new()
                .prefix("native spaces 路径 ")
                .tempdir()
                .unwrap(),
        )
    }
    fn record(&self) -> PathBuf {
        self.0.path().join("record.json")
    }
    fn options(&self, mode: &str) -> ProcessOptions {
        let python = std::env::var_os("SIM_TEST_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| which::which("python").expect("independent Windows test Python"));
        ProcessOptions {
            argv: vec![
                python.into_os_string(),
                "-u".into(),
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("tests/support/native_process_windows_child.py")
                    .into_os_string(),
                mode.into(),
                self.record().into_os_string(),
                LITERAL.into(),
                "{qmp}".into(),
                "{uart}".into(),
            ],
            sockets: vec!["qmp".into(), "uart".into()],
            wait_sockets: None,
            startup_timeout: Duration::from_secs(5),
            host_deadline: Instant::now() + Duration::from_secs(10),
        }
    }
    fn recorded(&self) -> Value {
        serde_json::from_slice(&fs::read(self.record()).unwrap()).unwrap()
    }
}

fn handle(pid: u32) -> OwnedHandle {
    // SAFETY: OpenProcess returns a new owned handle for this fixture identity.
    let raw = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    assert!(
        !raw.is_null(),
        "OpenProcess({pid}): {}",
        std::io::Error::last_os_error()
    );
    unsafe { OwnedHandle::from_raw_handle(raw) }
}
fn exited(process: &OwnedHandle) {
    // Retaining the original process handle avoids relying on a reusable PID.
    assert_eq!(
        unsafe { WaitForSingleObject(process.as_raw_handle(), 0) },
        WAIT_OBJECT_0
    );
}
fn read_exact(stream: &mut TcpStream, expected: &[u8]) {
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut actual = vec![0; expected.len()];
    let mut offset = 0;
    while offset < actual.len() {
        match stream.read(&mut actual[offset..]) {
            Ok(0) => panic!("fixture stream closed"),
            Ok(count) => offset += count,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "fixture read timed out");
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(error) => panic!("fixture read: {error}"),
        }
    }
    assert_eq!(actual, expected);
}
fn start_error(options: ProcessOptions) -> Arc<Error> {
    match NativeProcess::start(options) {
        Err(error) => error,
        Ok(mut owner) => {
            let _ = owner.close();
            panic!("expected start failure");
        }
    }
}

#[test]
fn reserved_streams_preserve_binary_bytes_literal_argv_and_transfer_once() {
    let fixture = Fixture::new();
    let mut owner = NativeProcess::start(fixture.options("ready")).unwrap();
    let record = fixture.recorded();
    assert_eq!(record["literal"], LITERAL);
    assert_eq!(owner.id(), Some(record["pid"].as_u64().unwrap() as u32));
    let process = handle(owner.id().unwrap());
    let directory = owner.directory().unwrap().to_owned();
    let mut qmp = owner.take_stream("qmp").unwrap();
    let mut uart = owner.take_stream("uart").unwrap();
    read_exact(&mut qmp, b"transport only");
    read_exact(&mut uart, b"uart\0\xff");
    assert!(qmp.nodelay().unwrap() && uart.nodelay().unwrap());
    assert_eq!(
        qmp.local_addr().unwrap().port(),
        record["ports"][0].as_u64().unwrap() as u16
    );
    assert_eq!(
        owner.take_stream("qmp").unwrap_err().kind,
        ErrorKind::Admission
    );
    let mut empty = [0];
    assert_eq!(
        qmp.read(&mut empty).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    drop((qmp, uart));
    assert_eq!(owner.finish(Ok(42)).unwrap(), 42);
    exited(&process);
    assert!(
        !directory.exists(),
        "success preceded native temporary cleanup"
    );
}

#[test]
fn leader_exit_zero_is_terminal_and_job_cleanup_stops_its_live_descendant() {
    let fixture = Fixture::new();
    let mut owner = NativeProcess::start(fixture.options("descendant-exit0")).unwrap();
    let record = fixture.recorded();
    let parent = handle(owner.id().unwrap());
    let descendant = handle(record["descendant"].as_u64().unwrap() as u32);
    let directory = owner.directory().unwrap().to_owned();
    let mut qmp = owner.take_stream("qmp").unwrap();
    read_exact(&mut qmp, b"transport only");
    qmp.write_all(b"x").unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(parent.as_raw_handle(), 3000) },
        WAIT_OBJECT_0
    );
    let primary = owner.check().unwrap_err();
    assert_eq!(primary.kind, ErrorKind::Exited);
    assert!(primary.message.contains("status 0"), "{primary}");
    drop(qmp);
    assert!(Arc::ptr_eq(&primary, &owner.close().unwrap_err()));
    exited(&parent);
    exited(&descendant);
    assert!(!directory.exists());
}

#[test]
fn startup_exit_retains_only_the_bounded_stderr_tail() {
    let fixture = Fixture::new();
    let error = start_error(fixture.options("exit0"));
    assert_eq!(error.kind, ErrorKind::Exited);
    assert!(error.message.contains("status 0"));
    let notes = error.notes();
    let tail = notes
        .iter()
        .find_map(|note| note.strip_prefix("native stderr tail:\n"))
        .unwrap();
    assert_eq!(tail.len(), 12000);
    assert!(!tail.contains("discard-prefix"));
    assert!(tail.ends_with("retained-tail\r\n") || tail.ends_with("retained-tail\n"));
}

#[test]
fn deadlines_reject_before_spawn_and_stop_a_nonconnecting_child() {
    let fixture = Fixture::new();
    let mut options = fixture.options("ready");
    options.host_deadline = Instant::now();
    assert_eq!(start_error(options).kind, ErrorKind::Deadline);
    assert!(!fixture.record().exists());

    let mut options = fixture.options("idle");
    options.startup_timeout = Duration::from_secs(1);
    let before = Instant::now();
    assert_eq!(start_error(options).kind, ErrorKind::Deadline);
    assert!(before.elapsed() < Duration::from_secs(4));
    let pid = fixture.recorded()["pid"].as_u64().unwrap() as u32;
    let raw = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if !raw.is_null() {
        exited(&unsafe { OwnedHandle::from_raw_handle(raw) });
    }
}

#[test]
fn consumer_primary_survives_independent_cleanup_error_and_drop_stops_tree() {
    let fixture = Fixture::new();
    let mut owner = NativeProcess::start(fixture.options("descendant")).unwrap();
    let record = fixture.recorded();
    let parent = handle(owner.id().unwrap());
    let descendant = handle(record["descendant"].as_u64().unwrap() as u32);
    let directory = owner.directory().unwrap().to_owned();
    fs::remove_file(owner.stderr_path()).unwrap();
    let primary = Arc::new(Error::new(ErrorKind::Io, "independent consumer failure"));
    assert!(Arc::ptr_eq(
        &primary,
        &owner.finish::<()>(Err(primary.clone())).unwrap_err()
    ));
    assert!(primary
        .notes()
        .iter()
        .any(|note| note.contains("native stderr read")));
    exited(&parent);
    exited(&descendant);
    assert!(!directory.exists());

    let fixture = Fixture::new();
    let owner = NativeProcess::start(fixture.options("ready")).unwrap();
    let parent = handle(owner.id().unwrap());
    let directory = owner.directory().unwrap().to_owned();
    drop(owner);
    exited(&parent);
    assert!(!directory.exists());
}

#[test]
fn final_active_check_ends_budget_before_independent_cleanup() {
    let fixture = Fixture::new();
    let mut owner = NativeProcess::start(fixture.options("ready")).unwrap();
    let parent = handle(owner.id().unwrap());
    owner
        .check_with_deadline(Instant::now() + Duration::from_millis(50))
        .unwrap();
    owner.begin_cleanup().unwrap();
    std::thread::sleep(Duration::from_millis(80));
    owner.close().unwrap();
    exited(&parent);
}

#[test]
fn admission_rejects_invalid_names_and_nul_before_child_effects() {
    let fixture = Fixture::new();
    let mut options = fixture.options("ready");
    options.sockets = vec!["qmp".into(), "qmp".into()];
    assert_eq!(start_error(options).kind, ErrorKind::Admission);
    let mut options = fixture.options("ready");
    options.argv.push(OsString::from("NUL\0argument"));
    assert_eq!(start_error(options).kind, ErrorKind::Admission);
    let mut options = fixture.options("ready");
    options.argv[0] = fixture
        .0
        .path()
        .join("missing executable.exe")
        .into_os_string();
    assert!(start_error(options).message.contains("native spawn"));
    assert!(!fixture.record().exists());
}
