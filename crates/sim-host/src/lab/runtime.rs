//! One free-running native QEMU owner, shared by console and Web consumers.
use super::channels::{AdcValue, ChannelEvent, LabChannels};
use crate::{
    boards,
    can::CanFrame,
    launch,
    native_process::{NativeProcess, ProcessOptions},
    qmp::{commands, QmpSession},
    transports::{fixed84::CanStream, stream::StreamTransport},
};
use rustix::event::{poll, PollFd, PollFlags, Timespec};
use serde_json::{json, Value};
use sim_core::{Error, Failure, Result};
use std::{
    collections::BTreeSet,
    fmt,
    os::{fd::BorrowedFd, unix::net::UnixStream},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}
fn lower(error: impl std::error::Error + Send + Sync + 'static) -> Failure {
    Arc::new(error)
}

#[derive(Clone, Debug)]
pub struct LabOptions {
    pub firmware: PathBuf,
    pub qemu: Option<PathBuf>,
    pub uarts: Vec<String>,
    pub cans: Vec<String>,
    pub adc: bool,
    pub startup_timeout: Duration,
    pub timeout: Duration,
    pub qmp_path: Option<PathBuf>,
    pub gdb_port: Option<u16>,
    pub cancellation: Option<Arc<AtomicBool>>,
}
impl LabOptions {
    pub fn new(firmware: impl Into<PathBuf>) -> Self {
        Self {
            firmware: firmware.into(),
            qemu: None,
            uarts: vec!["uart1".into()],
            cans: vec![],
            adc: false,
            startup_timeout: Duration::from_secs(10),
            timeout: Duration::from_secs(30),
            qmp_path: None,
            gdb_port: None,
            cancellation: None,
        }
    }
    fn admit(&self) -> Result<(PathBuf, PathBuf)> {
        if self.startup_timeout.is_zero()
            || self.timeout < self.startup_timeout
            || Instant::now().checked_add(self.timeout).is_none()
        {
            return Err(admission(
                "Lab requires positive startup timeout <= total host timeout",
            ));
        }
        for (ports, allowed) in [
            (&self.uarts, boards::launch::UARTS),
            (&self.cans, boards::launch::CANS),
        ] {
            let mut seen = BTreeSet::new();
            if ports
                .iter()
                .any(|port| !allowed.contains(&port.as_str()) || !seen.insert(port))
            {
                return Err(admission(
                    "Lab channels must be distinct supported board ports",
                ));
            }
        }
        if self.gdb_port == Some(0) {
            return Err(admission("GDB port requires 1..65535"));
        }
        let firmware = launch::readable_file(&self.firmware, "firmware ELF").map_err(lower)?;
        let qemu = launch::resolve_qemu(self.qemu.clone()).map_err(lower)?;
        Ok((firmware, qemu))
    }
}
#[derive(Clone, Debug)]
pub enum LabInput {
    Uart { endpoint: String, data: Vec<u8> },
    Can { endpoint: String, frame: CanFrame },
    Key(bool),
    Gpio { pin: String, level: bool },
    Adc { channel: u16, value: AdcValue },
}
#[derive(Debug)]
pub struct AdcPausedError;
impl fmt::Display for AdcPausedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ADC input requires running QEMU; use cont first")
    }
}
impl std::error::Error for AdcPausedError {}
#[derive(Debug)]
pub struct LabError {
    pub primary: Failure,
    pub notes: Vec<String>,
}
impl fmt::Display for LabError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.primary.fmt(f)
    }
}
impl std::error::Error for LabError {}
impl From<Failure> for LabError {
    fn from(primary: Failure) -> Self {
        Self {
            primary,
            notes: vec![],
        }
    }
}

pub struct ConnectedLab<'a> {
    owner: &'a mut NativeProcess,
    qmp: &'a mut QmpSession,
    runtime: &'a tokio::runtime::Runtime,
    channels: LabChannels<'a, UnixStream>,
    startup_deadline: Instant,
    host_deadline: Instant,
    startup: bool,
    cancellation: Option<Arc<AtomicBool>>,
    last_adc_ns: u64,
}
impl ConnectedLab<'_> {
    fn deadline(&self) -> Instant {
        if self.startup {
            self.startup_deadline.min(self.host_deadline)
        } else {
            self.host_deadline
        }
    }
    pub fn check(&mut self) -> Result<()> {
        if self
            .cancellation
            .as_ref()
            .is_some_and(|flag| flag.load(Ordering::Relaxed))
        {
            return Err(Arc::new(Error::Interrupted("Lab interrupted".into())));
        }
        self.owner
            .check_with_deadline(self.deadline())
            .map_err(|error| error as Failure)
    }
    pub fn remaining(&mut self) -> Result<Duration> {
        self.check()?;
        Ok(self.deadline().saturating_duration_since(Instant::now()))
    }
    pub fn pid(&self) -> u32 {
        self.owner.id().expect("live Lab process")
    }
    pub fn directory(&self) -> PathBuf {
        self.owner
            .directory()
            .expect("live Lab directory")
            .to_owned()
    }
    pub fn command<C: commands::QmpCommand>(&mut self, command: C) -> Result<C::Ok> {
        self.check()?;
        let value = self
            .runtime
            .block_on(self.qmp.command(command, self.deadline()))
            .map_err(|error| error as Failure)?;
        self.check()?;
        Ok(value)
    }
    pub fn snapshot(&mut self) -> Result<Value> {
        let value = self.command(commands::qom_get {
            path: "/machine".into(),
            property: "board-state".into(),
        })?;
        boards::validate_snapshot(&value)?;
        Ok(value)
    }
    pub fn set_property(&mut self, name: &str, value: Value) -> Result<()> {
        self.command(commands::qom_set {
            path: "/machine".into(),
            property: name.into(),
            value,
        })?;
        Ok(())
    }
    pub fn pause(&mut self) -> Result<()> {
        self.command(commands::stop {})?;
        Ok(())
    }
    pub fn resume(&mut self) -> Result<()> {
        self.command(commands::cont {})?;
        Ok(())
    }
    pub fn set_vin(&mut self, millivolts: u32) -> Result<()> {
        self.set_property("vin-mv", json!(millivolts.to_string()))
    }
    /// Snapshot after each channel read supplies its separate native upper bound.
    pub fn pump(&mut self) -> Result<(Value, Vec<ChannelEvent>)> {
        let before = self.snapshot()?["timestamp_ns"].as_u64().unwrap();
        let events = self
            .channels
            .service(before, true)
            .map_err(|error| error as Failure)?;
        let after = self.snapshot()?;
        if after["timestamp_ns"].as_u64().unwrap() < before {
            return Err(state("QEMU virtual time moved backwards"));
        }
        Ok((after, events))
    }
    /// Wait only for host readiness. QEMU owns every virtual-time advance.
    pub fn wait(&mut self, input: Option<BorrowedFd<'_>>, maximum: Duration) -> Result<bool> {
        self.check()?;
        let descriptors = self.channels.descriptors();
        let mut pollfds = Vec::new();
        if let Some(fd) = input.as_ref() {
            pollfds.push(PollFd::new(fd, PollFlags::IN));
        }
        for fd in &descriptors {
            pollfds.push(PollFd::new(fd, PollFlags::IN));
        }
        let exit_fd = self.owner.exit_fd();
        if let Some(fd) = exit_fd.as_ref() {
            pollfds.push(PollFd::new(fd, PollFlags::IN));
        }
        let remaining = self
            .deadline()
            .saturating_duration_since(Instant::now())
            .min(maximum);
        let timeout = Timespec::try_from(remaining).map_err(lower)?;
        match poll(&mut pollfds, Some(&timeout)) {
            Ok(_) => (),
            Err(rustix::io::Errno::INTR) => (),
            Err(error) => return Err(lower(error)),
        }
        let readable = input.is_some()
            && pollfds[0]
                .revents()
                .intersects(PollFlags::IN | PollFlags::HUP);
        drop(pollfds);
        drop(descriptors);
        self.check()?;
        Ok(readable)
    }
    /// Returned time observes admission; it does not attest guest consumption.
    pub fn inject(&mut self, input: LabInput) -> Result<u64> {
        if let LabInput::Gpio { pin, .. } = &input {
            boards::gpio_pin(pin)?;
        }
        let mut stamp = self.snapshot()?["timestamp_ns"].as_u64().unwrap();
        match input {
            LabInput::Uart { endpoint, data } => self
                .channels
                .send_uart(&endpoint, &data)
                .map_err(|error| error as Failure)?,
            LabInput::Can { endpoint, frame } => self
                .channels
                .send_can(&endpoint, frame.with_timestamp(stamp))
                .map_err(|error| error as Failure)?,
            LabInput::Key(pressed) => self.set_property("user-key", json!(pressed))?,
            LabInput::Gpio { pin, level } => {
                self.set_property("gpio-input", json!(format!("{pin}={}", u8::from(level))))?
            }
            LabInput::Adc { channel, value } => {
                loop {
                    if !self.command(commands::query_status {})?.running {
                        return Err(Arc::new(AdcPausedError));
                    }
                    if stamp > self.last_adc_ns {
                        break;
                    }
                    self.wait(None, Duration::from_millis(5))?;
                    stamp = self.snapshot()?["timestamp_ns"].as_u64().unwrap();
                }
                self.channels
                    .send_adc(channel, value, stamp)
                    .map_err(|error| error as Failure)?;
                self.last_adc_ns = stamp;
            }
        }
        self.check()?;
        Ok(stamp)
    }
}

fn cleanup_error(
    primary: &mut Option<Failure>,
    notes: &mut Vec<String>,
    label: &str,
    error: Failure,
) {
    if let Some(first) = primary {
        if !Arc::ptr_eq(first, &error) {
            notes.push(format!("{label}: {error}"));
        }
    } else {
        *primary = Some(error);
    }
}
/// Run a consumer with exclusive borrowed peers, then close each peer and the
/// native process before publishing its result. No Simulation is constructed.
pub fn with_lab<T>(
    options: LabOptions,
    consumer: impl FnOnce(&mut ConnectedLab<'_>) -> Result<T>,
) -> std::result::Result<T, LabError> {
    let (firmware, qemu) = options.admit()?;
    let streams: Vec<_> = options
        .uarts
        .iter()
        .chain(&options.cans)
        .cloned()
        .chain(options.adc.then_some("cosim".into()))
        .collect();
    let argv = launch::native_qemu_command(
        &qemu,
        boards::launch::machine_arguments(&firmware),
        boards::launch::SERIAL_SLOTS,
        &streams,
        options.qmp_path.as_deref(),
        options.gdb_port,
    )
    .map_err(|error| LabError::from(lower(error)))?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| LabError::from(lower(error)))?;
    let start = Instant::now();
    let host_deadline = start + options.timeout;
    let mut owner = NativeProcess::start(ProcessOptions {
        argv,
        sockets: std::iter::once("qmp".into())
            .chain(streams.clone())
            .collect(),
        wait_sockets: None,
        startup_timeout: options.startup_timeout,
        host_deadline,
    })
    .map_err(|error| LabError {
        notes: error.notes(),
        primary: error,
    })?;
    let startup_deadline = owner.started() + options.startup_timeout;
    let mut qmp = None;
    let mut uarts = Vec::new();
    let mut cans = Vec::new();
    let mut cosim = None;
    let execution = (|| -> Result<T> {
        owner
            .check_with_deadline(startup_deadline.min(host_deadline))
            .map_err(|error| error as Failure)?;
        qmp = Some(
            runtime
                .block_on(QmpSession::connect_uds(
                    owner.socket("qmp").unwrap(),
                    startup_deadline.min(host_deadline),
                ))
                .map_err(|error| error as Failure)?,
        );
        for name in &streams {
            owner
                .check_with_deadline(startup_deadline.min(host_deadline))
                .map_err(|error| error as Failure)?;
            let deadline = startup_deadline
                .min(host_deadline)
                .min(Instant::now() + Duration::from_secs(2));
            let stream = runtime
                .block_on(async {
                    tokio::time::timeout_at(
                        deadline.into(),
                        tokio::net::UnixStream::connect(owner.socket(name).unwrap()),
                    )
                    .await
                })
                .map_err(|_| {
                    Arc::new(Error::Deadline("Lab stream connection expired".into())) as Failure
                })?
                .map_err(lower)?
                .into_std()
                .map_err(lower)?;
            if name == "cosim" {
                cosim = Some(StreamTransport::new(stream, 256).map_err(|error| error as Failure)?);
            } else if options.cans.contains(name) {
                cans.push((
                    name.clone(),
                    CanStream::new(stream, 256).map_err(|error| error as Failure)?,
                ));
            } else {
                uarts.push((
                    name.clone(),
                    StreamTransport::new(stream, 256).map_err(|error| error as Failure)?,
                ));
            }
        }
        let channels = LabChannels::new(
            uarts
                .iter_mut()
                .map(|(name, stream)| (name.clone(), stream))
                .collect(),
            cans.iter_mut()
                .map(|(name, stream)| (name.clone(), stream))
                .collect(),
            cosim.as_mut(),
        )
        .map_err(|error| error as Failure)?;
        let mut lab = ConnectedLab {
            owner: &mut owner,
            qmp: qmp.as_mut().unwrap(),
            runtime: &runtime,
            channels,
            startup_deadline,
            host_deadline,
            startup: true,
            cancellation: options.cancellation.clone(),
            last_adc_ns: 0,
        };
        while !lab
            .channels
            .consume_opening()
            .map_err(|error| error as Failure)?
        {
            lab.wait(None, Duration::from_millis(5))?;
        }
        let stamp = lab.snapshot()?["timestamp_ns"].as_u64().unwrap();
        lab.channels
            .reset(stamp)
            .map_err(|error| error as Failure)?;
        lab.last_adc_ns = stamp;
        lab.startup = false;
        let result = consumer(&mut lab)?;
        lab.check()?;
        Ok(result)
    })();
    let (value, mut primary) = match execution {
        Ok(value) => (Some(value), None),
        Err(error) => (None, Some(error)),
    };
    let mut notes = Vec::new();
    if let Err(error) = owner.begin_cleanup() {
        cleanup_error(&mut primary, &mut notes, "Lab final active check", error);
    }
    if let Some(qmp) = &mut qmp {
        if let Err(error) = runtime.block_on(qmp.close()) {
            notes.extend(error.notes());
            cleanup_error(&mut primary, &mut notes, "Lab QMP cleanup", error);
        }
    }
    drop(qmp);
    drop(cosim);
    drop(cans);
    drop(uarts);
    if let Err(error) = owner.close() {
        notes.extend(error.notes());
        cleanup_error(&mut primary, &mut notes, "Lab native cleanup", error);
    }
    if let Some(primary) = primary {
        Err(LabError { primary, notes })
    } else {
        Ok(value.expect("successful consumer result"))
    }
}
