//! Native process and local IPC ownership shared by host consumers.
//!
//! Platform adapters own child lifetime and endpoint acquisition. Protocol peers
//! stay with the consumer and must close before the owner reports success.

use std::{
    collections::BTreeSet,
    ffi::OsString,
    fmt,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

const STOP_LIMIT: Duration = Duration::from_secs(1);
const STARTUP_POLL: Duration = Duration::from_millis(10);
const STDERR_TAIL: u64 = 12_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Admission,
    Interrupted,
    Deadline,
    Exited,
    Io,
    Cleanup,
    Closed,
}

#[derive(Debug)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
    notes: Mutex<Vec<String>>,
    cleanup_failed: AtomicBool,
}
pub type Failure = Arc<Error>;
pub type Result<T> = std::result::Result<T, Failure>;

impl Error {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            notes: Mutex::new(Vec::new()),
            cleanup_failed: AtomicBool::new(kind == ErrorKind::Cleanup),
        }
    }
    pub fn cleanup_failed(&self) -> bool {
        self.cleanup_failed.load(Ordering::Relaxed)
    }
    pub fn notes(&self) -> Vec<String> {
        self.notes.lock().unwrap().clone()
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.message.fmt(f)
    }
}
impl std::error::Error for Error {}

pub struct ProcessOptions {
    pub argv: Vec<OsString>,
    pub sockets: Vec<String>,
    /// None waits for every declared socket; Some([]) waits for none.
    pub wait_sockets: Option<Vec<String>>,
    pub startup_timeout: Duration,
    pub host_deadline: Instant,
}

impl ProcessOptions {
    fn validate(&self) -> Result<()> {
        let reject = |message| Err(Arc::new(Error::new(ErrorKind::Admission, message)));
        if self.argv.is_empty()
            || self.argv[0].is_empty()
            || self
                .argv
                .iter()
                .any(|arg| arg.as_encoded_bytes().contains(&0))
        {
            return reject("a nonempty literal argv without NUL is required");
        }
        let mut names = BTreeSet::new();
        if self.sockets.is_empty()
            || self.sockets.iter().any(|name| {
                !(1..=16).contains(&name.len())
                    || !name.bytes().enumerate().all(|(index, byte)| {
                        byte.is_ascii_lowercase()
                            || index > 0 && (byte.is_ascii_digit() || byte == b'_')
                    })
                    || !names.insert(name)
            })
        {
            return reject("socket names must be unique short ASCII identifiers");
        }
        if self
            .wait_sockets
            .as_ref()
            .is_some_and(|names| names.iter().any(|name| !self.sockets.contains(name)))
        {
            return reject("wait sockets must be declared socket names");
        }
        if self.startup_timeout.is_zero()
            || Instant::now().checked_add(self.startup_timeout).is_none()
        {
            return reject("startup timeout must be finite and positive");
        }
        if Instant::now() >= self.host_deadline {
            return Err(Arc::new(Error::new(
                ErrorKind::Deadline,
                "native host deadline expired before spawn",
            )));
        }
        Ok(())
    }
}

fn io_failure(label: &str, error: impl fmt::Display) -> Failure {
    Arc::new(Error::new(ErrorKind::Io, format!("{label}: {error}")))
}

fn check_cancellation(cancellation: Option<&AtomicBool>) -> Result<()> {
    if cancellation.is_some_and(|flag| flag.load(Ordering::Relaxed)) {
        return Err(Arc::new(Error::new(
            ErrorKind::Interrupted,
            "native startup interrupted",
        )));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
pub use linux::NativeProcess;
#[cfg(all(target_os = "linux", feature = "host-processes"))]
pub(crate) use linux::{exit_status, stop_child_group};

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::NativeProcess;
