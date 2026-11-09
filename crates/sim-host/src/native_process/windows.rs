//! Windows native process tree and reserved loopback stream owner.
use super::{
    io_failure, Error, ErrorKind, Failure, ProcessOptions, Result, STARTUP_POLL, STDERR_TAIL,
    STOP_LIMIT,
};
#[path = "windows_job.rs"]
mod job;
use std::{
    collections::BTreeMap,
    ffi::{OsStr, OsString},
    fs::File,
    io::{self, Read, Seek, SeekFrom},
    net::{TcpListener, TcpStream},
    os::windows::{
        ffi::{OsStrExt, OsStringExt},
        io::{AsHandle, BorrowedHandle},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Arc,
    time::Instant,
};
use tempfile::TempDir;
use wait_timeout::ChildExt;
use windows_sys::Win32::System::{
    Console::{GenerateConsoleCtrlEvent, CTRL_BREAK_EVENT},
    Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_SUSPENDED},
};

pub struct NativeProcess {
    child: Option<Child>,
    job: Option<job::Job>,
    job_assigned: bool,
    listeners: BTreeMap<String, TcpListener>,
    streams: BTreeMap<String, TcpStream>,
    temporary: Option<TempDir>,
    stderr: Option<File>,
    stderr_path: PathBuf,
    started: Instant,
    deadline: Instant,
    first: Option<Failure>,
    cleanup_started: bool,
    closed: bool,
}

fn expand(value: &OsStr, ports: &BTreeMap<String, u16>) -> OsString {
    let mut units: Vec<_> = value.encode_wide().collect();
    for (name, port) in ports {
        let pattern: Vec<_> = format!("{{{name}}}").encode_utf16().collect();
        let replacement: Vec<_> = port.to_string().encode_utf16().collect();
        let mut replaced = Vec::new();
        let mut rest = units.as_slice();
        while let Some(offset) = rest.windows(pattern.len()).position(|part| part == pattern) {
            replaced.extend_from_slice(&rest[..offset]);
            replaced.extend_from_slice(&replacement);
            rest = &rest[offset + pattern.len()..];
        }
        replaced.extend_from_slice(rest);
        units = replaced;
    }
    OsString::from_wide(&units)
}

impl NativeProcess {
    pub fn start(options: ProcessOptions) -> Result<Self> {
        options.validate()?;
        let started = Instant::now();
        let startup_deadline = (started + options.startup_timeout).min(options.host_deadline);
        let temporary = tempfile::Builder::new()
            .prefix("dm-qemu.native.")
            .tempdir()
            .map_err(|error| io_failure("native temporary directory", error))?;
        let stderr_path = temporary.path().join("qemu.stderr");
        let stderr =
            File::create(&stderr_path).map_err(|error| io_failure("native stderr", error))?;
        let mut listeners = BTreeMap::new();
        let mut ports = BTreeMap::new();
        for name in &options.sockets {
            let listener = TcpListener::bind(("127.0.0.1", 0))
                .map_err(|error| io_failure("native loopback bind", error))?;
            listener
                .set_nonblocking(true)
                .map_err(|error| io_failure("native listener nonblocking", error))?;
            ports.insert(
                name.clone(),
                listener
                    .local_addr()
                    .map_err(|error| io_failure("native listener address", error))?
                    .port(),
            );
            listeners.insert(name.clone(), listener);
        }
        let argv: Vec<_> = options.argv.iter().map(|arg| expand(arg, &ports)).collect();
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
            .creation_flags(CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP);
        let mut owner = Self {
            child: None,
            job: Some(job::Job::new().map_err(|error| io_failure("native job create", error))?),
            job_assigned: false,
            listeners,
            streams: BTreeMap::new(),
            temporary: Some(temporary),
            stderr: Some(stderr),
            stderr_path,
            started,
            deadline: options.host_deadline,
            first: None,
            cleanup_started: false,
            closed: false,
        };
        let setup = (|| -> Result<()> {
            owner.check_with_deadline(startup_deadline)?;
            // Register the suspended child before assignment/resume can fail.
            owner.child = Some(
                command
                    .spawn()
                    .map_err(|error| io_failure("native spawn", error))?,
            );
            drop(command);
            let child = owner.child.as_ref().unwrap();
            owner
                .job
                .as_ref()
                .unwrap()
                .assign(child)
                .map_err(|error| io_failure("native job assignment", error))?;
            owner.job_assigned = true;
            job::resume(child).map_err(|error| io_failure("native child resume", error))?;
            for name in options.wait_sockets.as_ref().unwrap_or(&options.sockets) {
                owner.accept(name, startup_deadline)?;
            }
            owner.check_with_deadline(options.host_deadline)?;
            Ok(())
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

    fn accept(&mut self, name: &str, deadline: Instant) -> Result<()> {
        if self.streams.contains_key(name) {
            return Ok(());
        }
        loop {
            self.check()?;
            if Instant::now() >= deadline {
                return Err(Arc::new(Error::new(
                    ErrorKind::Deadline,
                    "native stream startup deadline expired",
                )));
            }
            let listener = self.listeners.get(name).ok_or_else(|| {
                Arc::new(Error::new(
                    ErrorKind::Admission,
                    "unknown or already taken native stream",
                ))
            })?;
            match listener.accept() {
                Ok((stream, _)) => {
                    stream
                        .set_nonblocking(true)
                        .map_err(|error| io_failure("native stream nonblocking", error))?;
                    stream
                        .set_nodelay(true)
                        .map_err(|error| io_failure("native stream TCP_NODELAY", error))?;
                    self.streams.insert(name.to_owned(), stream);
                    self.listeners.remove(name);
                    self.check()?;
                    return Ok(());
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => (),
                Err(error) => return Err(io_failure("native stream accept", error)),
            }
            std::thread::sleep(
                STARTUP_POLL.min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }

    /// Transfer a connected nonblocking stream once. The consumer owns its IO
    /// and must close it before asking this owner to finish process cleanup.
    pub fn take_stream(&mut self, name: &str) -> Result<TcpStream> {
        self.check()?;
        if !self.streams.contains_key(name) {
            self.accept(name, self.deadline)?;
        }
        Ok(self.streams.remove(name).expect("accepted stream"))
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
    pub fn stderr_path(&self) -> &Path {
        &self.stderr_path
    }
    /// Borrowed process notification. The owner retains the process and job.
    pub fn exit_handle(&self) -> Option<BorrowedHandle<'_>> {
        self.child.as_ref().map(AsHandle::as_handle)
    }
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
        if let Some(child) = &mut self.child {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let error = Arc::new(Error::new(
                        ErrorKind::Exited,
                        format!(
                            "native process exited (status {})",
                            status.code().unwrap_or(1)
                        ),
                    ));
                    return Err(self.latch(error));
                }
                Ok(None) => (),
                Err(error) => return Err(self.latch(io_failure("native exit observation", error))),
            }
        }
        Ok(())
    }
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

    fn stop(&mut self) -> Vec<String> {
        let mut errors = Vec::new();
        if let Some(child) = &mut self.child {
            // Targeted CTRL_BREAK requires an attached console. Services/GUI
            // parents may have none; job termination is the unconditional path.
            // SAFETY: CREATE_NEW_PROCESS_GROUP made this child's PID its group.
            if unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, child.id()) } != 0 {
                if let Err(error) = child.wait_timeout(STOP_LIMIT) {
                    errors.push(format!("native CTRL_BREAK wait: {error}"));
                }
            }
        }
        let job_terminated = if let Some(job) = &self.job {
            match job.terminate() {
                Ok(()) => true,
                Err(error) => {
                    errors.push(format!("native job terminate: {error}"));
                    false
                }
            }
        } else {
            false
        };
        // Assignment may fail while the child is still suspended, outside the
        // job. Terminate that original process independently; after successful
        // job termination it is already stopping and must only be waited for.
        if let Some(child) = self
            .child
            .as_mut()
            .filter(|_| !self.job_assigned || !job_terminated)
        {
            match child.try_wait() {
                Ok(Some(_)) => (),
                Ok(None) => {
                    if let Err(error) = child.kill() {
                        errors.push(format!("native child terminate: {error}"));
                    }
                }
                Err(error) => errors.push(format!("native exit observation: {error}")),
            }
        }
        let deadline = Instant::now() + STOP_LIMIT;
        loop {
            let reaped = match self.child.as_mut().map(Child::try_wait).transpose() {
                Ok(status) => status.is_none_or(|value| value.is_some()),
                Err(error) => {
                    errors.push(format!("native reap: {error}"));
                    break;
                }
            };
            let empty = match self.job.as_ref().map(job::Job::empty).transpose() {
                Ok(empty) => empty.unwrap_or(true),
                Err(error) => {
                    errors.push(format!("native job accounting: {error}"));
                    break;
                }
            };
            if reaped && empty {
                break;
            }
            if Instant::now() >= deadline {
                errors.push("native process tree remained alive after termination deadline".into());
                break;
            }
            std::thread::sleep(
                STARTUP_POLL.min(deadline.saturating_duration_since(Instant::now())),
            );
        }
        self.child.take();
        // KILL_ON_JOB_CLOSE remains a fallback if accounting or waiting failed.
        self.job.take();
        errors
    }

    pub fn close(&mut self) -> Result<()> {
        if !self.closed {
            let _ = self.begin_cleanup();
            self.closed = true;
            self.streams.clear();
            self.listeners.clear();
            let mut errors = self.stop();
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
        self.first
            .as_ref()
            .map_or(Ok(()), |error| Err(error.clone()))
    }
}

impl Drop for NativeProcess {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
