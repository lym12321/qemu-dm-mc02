//! Linux process groups, pidfd exit observation and native Unix sockets.
use super::{
    io_failure, Error, ErrorKind, Failure, ProcessOptions, Result, STARTUP_POLL, STDERR_TAIL,
    STOP_LIMIT,
};

use rustix::{
    event::{poll, PollFd, PollFlags, Timespec},
    io::Errno,
    process::{
        kill_process_group, pidfd_open, waitid, Pid, PidfdFlags, Signal, WaitId, WaitIdOptions,
        WaitIdStatus,
    },
};
use std::{
    collections::BTreeMap,
    ffi::{OsStr, OsString},
    fs::{self, File},
    io::{self, Read, Seek, SeekFrom},
    os::{
        fd::{AsFd, BorrowedFd, OwnedFd},
        unix::{
            ffi::{OsStrExt, OsStringExt},
            fs::FileTypeExt,
            process::CommandExt,
        },
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Arc,
    time::Instant,
};
use tempfile::TempDir;
use wait_timeout::ChildExt;

pub struct NativeProcess {
    child: Option<Child>,
    pidfd: Option<OwnedFd>,
    temporary: Option<TempDir>,
    stderr: Option<File>,
    stderr_path: PathBuf,
    sockets: BTreeMap<String, PathBuf>,
    started: Instant,
    deadline: Instant,
    first: Option<Failure>,
    cleanup_started: bool,
    closed: bool,
}

fn expand(value: &OsStr, paths: &BTreeMap<String, PathBuf>) -> OsString {
    let mut bytes = value.as_bytes().to_vec();
    for (name, path) in paths {
        let pattern = format!("{{{name}}}");
        let mut replaced = Vec::new();
        let mut rest = bytes.as_slice();
        while let Some(offset) = rest
            .windows(pattern.len())
            .position(|part| part == pattern.as_bytes())
        {
            replaced.extend_from_slice(&rest[..offset]);
            replaced.extend_from_slice(path.as_os_str().as_bytes());
            rest = &rest[offset + pattern.len()..];
        }
        replaced.extend_from_slice(rest);
        bytes = replaced;
    }
    OsString::from_vec(bytes)
}

pub(crate) fn exit_status(pid: Pid) -> io::Result<Option<WaitIdStatus>> {
    // Keep the leader waitable until every group signal has been sent. Merely
    // observing exit must not release its PID/PGID for reuse before cleanup.
    waitid(
        WaitId::Pid(pid),
        WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
    )
    .map_err(From::from)
}

/// Stop the original process group before releasing the waitable leader's PID.
/// The caller owns the child and every pipe/file associated with it.
pub(crate) fn stop_child_group(
    child: &mut Child,
    exit_fd: Option<BorrowedFd<'_>>,
) -> (bool, Vec<String>) {
    let pid = Pid::from_child(child);
    let mut errors = Vec::new();
    for signal in [Signal::TERM, Signal::KILL] {
        if let Err(error) = kill_process_group(pid, signal) {
            if error != Errno::SRCH {
                errors.push(format!("native group {signal:?}: {error}"));
            }
        }
        if signal == Signal::TERM {
            if let Some(fd) = exit_fd {
                if let Err(error) = wait_pidfd(fd, Instant::now() + STOP_LIMIT) {
                    errors.push(format!("native TERM wait: {error}"));
                }
            }
        }
    }
    let reaped = match child.wait_timeout(STOP_LIMIT) {
        Ok(Some(_)) => true,
        Ok(None) => {
            errors.push("native process remained alive after TERM/KILL deadlines".into());
            false
        }
        Err(error) => {
            errors.push(format!("native reap: {error}"));
            false
        }
    };
    (reaped, errors)
}

fn wait_pidfd(fd: BorrowedFd<'_>, deadline: Instant) -> io::Result<()> {
    loop {
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Ok(());
        };
        let timeout = Timespec::try_from(remaining).map_err(io::Error::other)?;
        match poll(&mut [PollFd::new(&fd, PollFlags::IN)], Some(&timeout)) {
            Ok(_) => return Ok(()),
            Err(Errno::INTR) => continue,
            Err(error) => return Err(error.into()),
        }
    }
}

impl NativeProcess {
    pub fn start(options: ProcessOptions) -> Result<Self> {
        options.validate()?;
        let temporary = tempfile::Builder::new()
            .prefix("dm-qemu.native.")
            .tempdir_in("/tmp")
            .map_err(|error| io_failure("native temporary directory", error))?;
        let sockets: BTreeMap<_, _> = options
            .sockets
            .iter()
            .map(|name| (name.clone(), temporary.path().join(format!("{name}.sock"))))
            .collect();
        let wait_names = options.wait_sockets.as_ref().unwrap_or(&options.sockets);
        let stderr_path = temporary.path().join("qemu.stderr");
        let stderr =
            File::create(&stderr_path).map_err(|error| io_failure("native stderr", error))?;
        let argv: Vec<_> = options
            .argv
            .iter()
            .map(|arg| expand(arg, &sockets))
            .collect();
        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(
                stderr
                    .try_clone()
                    .map_err(|error| io_failure("native stderr clone", error))?,
            )
            .process_group(0);
        // Both budgets include the complete blocking spawn/exec handshake.
        let started = Instant::now();
        let startup_deadline = (started + options.startup_timeout).min(options.host_deadline);
        let mut owner = Self {
            child: None,
            pidfd: None,
            temporary: Some(temporary),
            stderr: Some(stderr),
            stderr_path,
            sockets,
            started,
            deadline: options.host_deadline,
            first: None,
            cleanup_started: false,
            closed: false,
        };
        let setup = (|| -> Result<()> {
            owner.check()?;
            // Register ownership immediately, before any fallible post-spawn work.
            owner.child = Some(
                command
                    .spawn()
                    .map_err(|error| io_failure("native spawn", error))?,
            );
            drop(command);
            let pid = owner.pid().expect("spawned child has a PID");
            owner.pidfd = Some(
                pidfd_open(pid, PidfdFlags::empty())
                    .map_err(|error| io_failure("native pidfd", error))?,
            );
            loop {
                owner.check()?;
                if Instant::now() >= startup_deadline {
                    return Err(Arc::new(Error::new(
                        ErrorKind::Deadline,
                        "native socket startup deadline expired",
                    )));
                }
                let mut ready = true;
                for name in wait_names {
                    match fs::metadata(&owner.sockets[name]) {
                        Ok(metadata) => ready &= metadata.file_type().is_socket(),
                        Err(error) if error.kind() == io::ErrorKind::NotFound => ready = false,
                        Err(error) => return Err(io_failure("native socket readiness", error)),
                    }
                }
                owner.check()?;
                if ready {
                    return Ok(());
                }
                std::thread::sleep(
                    STARTUP_POLL.min(startup_deadline.saturating_duration_since(Instant::now())),
                );
            }
        })();
        match setup {
            Ok(()) => Ok(owner),
            Err(error) => {
                owner.latch(error);
                owner.close()?;
                unreachable!("cleanup retains setup error")
            }
        }
    }

    fn latch(&mut self, error: Failure) -> Failure {
        self.first.get_or_insert(error).clone()
    }
    fn pid(&self) -> Option<Pid> {
        self.child
            .as_ref()
            .and_then(|child| i32::try_from(child.id()).ok())
            .and_then(Pid::from_raw)
    }
    pub fn id(&self) -> Option<u32> {
        self.child.as_ref().map(Child::id)
    }
    pub fn started(&self) -> Instant {
        self.started
    }
    pub fn directory(&self) -> Option<&Path> {
        self.temporary.as_ref().map(TempDir::path)
    }
    pub fn socket(&self, name: &str) -> Option<&Path> {
        self.sockets.get(name).map(PathBuf::as_path)
    }
    pub fn stderr_path(&self) -> &Path {
        &self.stderr_path
    }
    /// Borrowed readiness notification; the owner alone signals and reaps.
    pub fn exit_fd(&self) -> Option<BorrowedFd<'_>> {
        self.pidfd.as_ref().map(AsFd::as_fd)
    }

    /// Any pre-cleanup exit, including status zero, is terminal.
    /// Borrow the caller's current absolute budget (startup or active session).
    pub fn check_with_deadline(&mut self, deadline: Instant) -> Result<()> {
        self.deadline = deadline;
        self.check()
    }

    pub fn check(&mut self) -> Result<()> {
        if let Some(error) = &self.first {
            return Err(error.clone());
        }
        if self.closed || self.cleanup_started {
            return Err(Arc::new(Error::new(
                ErrorKind::Closed,
                "native process owner is closed or cleaning up",
            )));
        }
        if Instant::now() >= self.deadline {
            let error = Arc::new(Error::new(
                ErrorKind::Deadline,
                "native host deadline expired",
            ));
            return Err(self.latch(error));
        }
        if let Some(pid) = self.pid() {
            match exit_status(pid) {
                Ok(Some(status)) => {
                    let detail = status
                        .exit_status()
                        .map(|code| format!("status {code}"))
                        .unwrap_or_else(|| {
                            format!("signal {}", status.terminating_signal().unwrap_or_default())
                        });
                    let error = Arc::new(Error::new(
                        ErrorKind::Exited,
                        format!("native process exited ({detail})"),
                    ));
                    return Err(self.latch(error));
                }
                Ok(None) => (),
                Err(error) => {
                    let error = io_failure("native exit observation", error);
                    return Err(self.latch(error));
                }
            }
        }
        Ok(())
    }

    /// Keep the consumer's primary failure and add independent cleanup notes.
    pub fn finish<T>(&mut self, result: Result<T>) -> Result<T> {
        let value = match result {
            Ok(value) => Some(value),
            Err(error) => {
                self.latch(error);
                None
            }
        };
        self.close()?;
        Ok(value.expect("a failed consumer cannot pass cleanup"))
    }

    /// End the active budget before a consumer closes its own peers. Every
    /// subsequent resource uses its separate finite cleanup budget.
    pub fn begin_cleanup(&mut self) -> Result<()> {
        if !self.cleanup_started {
            let result = self.check();
            self.cleanup_started = true;
            return result;
        }
        self.first
            .as_ref()
            .map_or(Ok(()), |error| Err(error.clone()))
    }

    /// TERM and KILL each have their own finite resource cleanup budget.
    pub fn close(&mut self) -> Result<()> {
        if !self.closed {
            let _ = self.begin_cleanup();
            self.closed = true;
            let mut errors = Vec::new();
            if let Some(child) = &mut self.child {
                let (reaped, child_errors) =
                    stop_child_group(child, self.pidfd.as_ref().map(AsFd::as_fd));
                errors.extend(child_errors);
                if reaped {
                    self.child.take();
                }
            }
            self.pidfd.take();
            self.stderr.take();
            let tail = (|| -> io::Result<String> {
                let mut file = File::open(&self.stderr_path)?;
                let length = file.metadata()?.len();
                file.seek(SeekFrom::Start(length.saturating_sub(STDERR_TAIL)))?;
                let mut bytes = Vec::new();
                file.take(STDERR_TAIL).read_to_end(&mut bytes)?;
                Ok(String::from_utf8_lossy(&bytes).into_owned())
            })();
            if let Err(error) = &tail {
                errors.push(format!("native stderr read: {error}"));
            }
            if let Some(temporary) = self.temporary.take() {
                if let Err(error) = temporary.close() {
                    errors.push(format!("native temporary directory: {error}"));
                }
            }
            if self.first.is_none() && !errors.is_empty() {
                self.latch(Arc::new(Error::new(ErrorKind::Cleanup, errors.remove(0))));
            }
            if let Some(first) = &self.first {
                let mut notes = first.notes.lock().unwrap();
                notes.extend(errors);
                if let Ok(tail) = tail {
                    if !tail.is_empty() {
                        notes.push(format!("native stderr tail:\n{tail}"));
                    }
                }
            }
        }
        match &self.first {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}

impl Drop for NativeProcess {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
