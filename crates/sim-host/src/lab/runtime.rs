//! One free-running native QEMU owner, shared by console and Web consumers.
use super::{
    channels::{AdcValue, ChannelData, ChannelEvent, LabChannels},
    input::BorrowedInput,
};
use crate::{
    boards,
    can::CanFrame,
    launch,
    native_process::{NativeProcess, ProcessOptions},
    qmp::{QmpSession, commands},
    transports::{fixed84::CanStream, stream::StreamTransport},
};
use futures::FutureExt;
#[cfg(unix)]
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sim_core::{Error, Failure, Result};
#[cfg(windows)]
use std::net::TcpStream as NativeStream;
#[cfg(unix)]
use std::os::unix::net::UnixStream as NativeStream;
use std::{
    collections::BTreeSet,
    fmt,
    future::Future,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
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

/// Poll cancellation alongside acquisition without spawning another IO owner.
/// Dropping an unfinished negotiation also drops its not-yet-owned stream.
async fn startup_operation<T>(
    operation: impl Future<Output = Result<T>>,
    cancellation: Option<&AtomicBool>,
) -> Result<T> {
    let Some(flag) = cancellation else {
        return operation.await;
    };
    let interrupted = async {
        while !flag.load(Ordering::Relaxed) {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }
    .fuse();
    let operation = operation.fuse();
    futures::pin_mut!(operation, interrupted);
    futures::select_biased! {
        result = operation => result,
        () = interrupted => Err(Arc::new(Error::Interrupted("Lab interrupted".into()))),
    }
}

#[derive(Clone, Debug)]
pub struct LabOptions {
    pub firmware: PathBuf,
    pub qemu: Option<PathBuf>,
    pub uarts: Vec<String>,
    pub cans: Vec<String>,
    pub adc: bool,
    pub imu: ImuMode,
    pub usbip: Option<std::net::SocketAddr>,
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
            imu: ImuMode::Off,
            usbip: None,
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
/// A stationary board fixture; it does not provide calibrated physical motion.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ImuMode {
    #[default]
    Off,
    Stationary,
}
#[derive(Clone, Debug)]
pub enum LabInput {
    Uart { endpoint: String, data: Vec<u8> },
    Can { endpoint: String, frame: CanFrame },
    Key(bool),
    Gpio { pin: String, level: bool },
    Adc { channel: u16, value: AdcValue },
}
/// Commands shared by native library, console and presentation consumers.
#[derive(Clone, Debug)]
pub enum BoardCommand {
    Pause,
    Resume,
    UsbAttach,
    UsbDetach,
    Vin(u32),
    Input(LabInput),
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum StartupMode {
    #[default]
    Run,
    WaitGdb,
    User,
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
    pub cleanup_failed: bool,
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
            cleanup_failed: false,
        }
    }
}

pub struct ConnectedLab<'a> {
    owner: &'a mut NativeProcess,
    qmp: &'a mut QmpSession,
    runtime: &'a tokio::runtime::Runtime,
    channels: LabChannels<'a, NativeStream>,
    startup_deadline: Instant,
    host_deadline: Instant,
    startup: bool,
    cancellation: Option<Arc<AtomicBool>>,
    adc: bool,
    imu: ImuMode,
    gdb_enabled: bool,
    auto_user_release_ns: Option<u64>,
    usb: Option<crate::usbip::Exporter<'a, NativeStream>>,
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
            .map_err(|error| error as Failure)?;
        if let Some(usb) = &mut self.usb {
            usb.service().map_err(lower)?;
        }
        Ok(())
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
    pub fn attach_usb(&mut self) -> Result<()> {
        self.check()?;
        self.usb
            .as_mut()
            .ok_or_else(|| admission("USB/IP is disabled; start Lab with --usbip"))?
            .attach()
            .map_err(|e| admission(e.to_string()))
    }
    pub fn detach_usb(&mut self) -> Result<()> {
        self.check()?;
        self.usb
            .as_mut()
            .ok_or_else(|| admission("USB/IP is disabled"))?
            .detach()
            .map_err(|e| admission(e.to_string()))
    }
    pub fn usb_status(&self) -> Value {
        self.usb
            .as_ref()
            .map(|usb| usb.status())
            .unwrap_or_else(|| json!({"state":"disabled"}))
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
    /// Apply one already decoded command. Input timestamps describe host
    /// admission, never guest consumption.
    pub fn apply(&mut self, command: BoardCommand) -> Result<Option<u64>> {
        match command {
            BoardCommand::Pause => self.pause()?,
            BoardCommand::Resume => self.resume()?,
            BoardCommand::UsbAttach => self.attach_usb()?,
            BoardCommand::UsbDetach => self.detach_usb()?,
            BoardCommand::Vin(value) => self.set_vin(value)?,
            BoardCommand::Input(input) => return self.inject(input).map(Some),
        }
        Ok(None)
    }
    /// Configure startup on the reset-paused native machine. USER release is
    /// serviced by the existing pump using QEMU virtual time.
    pub fn start(&mut self, mode: StartupMode, hold: Duration) -> Result<()> {
        match mode {
            StartupMode::Run => self.resume(),
            StartupMode::WaitGdb => {
                if !self.gdb_enabled {
                    return Err(admission("Wait GDB startup requires a native GDB port"));
                }
                self.pause()
            }
            StartupMode::User => {
                let hold_ns = u64::try_from(hold.as_nanos())
                    .ok()
                    .filter(|value| *value != 0)
                    .ok_or_else(|| admission("USER hold must be positive and fit virtual time"))?;
                let stamp = self.snapshot()?["timestamp_ns"].as_u64().unwrap();
                let release_ns = stamp
                    .checked_add(hold_ns)
                    .ok_or_else(|| admission("USER release exceeds virtual time range"))?;
                self.set_property("user-key", json!(true))?;
                self.auto_user_release_ns = Some(release_ns);
                self.resume()
            }
        }
    }
    fn release_startup_user(&mut self, stamp: u64) -> Result<bool> {
        if self.auto_user_release_ns.is_some_and(|due| stamp >= due) {
            self.set_property("user-key", json!(false))?;
            self.auto_user_release_ns = None;
            return Ok(true);
        }
        Ok(false)
    }
    /// Snapshot after each channel read supplies its separate native upper bound.
    pub fn pump(&mut self) -> Result<(Value, Vec<ChannelEvent>)> {
        let before = self.snapshot()?["timestamp_ns"].as_u64().unwrap();
        self.release_startup_user(before)?;
        let events = self
            .channels
            .service(before, true)
            .map_err(|error| error as Failure)?;
        let mut after = self.snapshot()?;
        if after["timestamp_ns"].as_u64().unwrap() < before {
            return Err(state("QEMU virtual time moved backwards"));
        }
        if self.release_startup_user(after["timestamp_ns"].as_u64().unwrap())? {
            after = self.snapshot()?;
        }
        let stamp = after["timestamp_ns"].as_u64().unwrap();
        if events
            .iter()
            .any(|event| matches!(event.data, ChannelData::Reset))
        {
            // Only this native owner reinitializes the frozen shared input
            // lane; the event remains visible to each consumer after success.
            self.channels
                .reset_input(stamp)
                .map_err(|error| error as Failure)?;
        }
        if self.imu == ImuMode::Stationary
            && self.command(commands::query_status {})?.running
            && self
                .channels
                .input_available_at(stamp)
                .map_err(|error| error as Failure)?
        {
            self.channels
                .send_imu(
                    &crate::signals::ImuSample {
                        gyro_rad_s: [0.0; 3],
                        accel_m_s2: [0.0, 0.0, 9.80665],
                    },
                    stamp,
                )
                .map_err(|error| error as Failure)?;
        }
        Ok((after, events))
    }
    /// Wait only for host readiness. QEMU owns every virtual-time advance.
    #[cfg(unix)]
    pub fn wait(&mut self, input: Option<BorrowedInput<'_>>, maximum: Duration) -> Result<bool> {
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
    /// Windows console handles and Winsock streams use different readiness
    /// APIs. A bounded host wait lets the same owner pump each nonblocking
    /// stream and observe cancellation/child exit at least every five ms.
    #[cfg(windows)]
    pub fn wait(&mut self, input: Option<BorrowedInput<'_>>, maximum: Duration) -> Result<bool> {
        self.check()?;
        let readable = || {
            input
                .map(super::input::ready)
                .transpose()
                .map(|value| value.unwrap_or(false))
                .map_err(lower)
        };
        if !readable()? {
            let remaining = self.deadline().saturating_duration_since(Instant::now());
            std::thread::sleep(maximum.min(remaining).min(Duration::from_millis(5)));
        }
        let ready = readable()?;
        self.check()?;
        Ok(ready)
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
            LabInput::Key(pressed) => {
                self.set_property("user-key", json!(pressed))?;
                self.auto_user_release_ns = None;
            }
            LabInput::Gpio { pin, level } => {
                self.set_property("gpio-input", json!(format!("{pin}={}", u8::from(level))))?
            }
            LabInput::Adc { channel, value } => {
                if !self.adc {
                    return Err(admission("ADC co-sim is disabled"));
                }
                loop {
                    if !self.command(commands::query_status {})?.running {
                        return Err(Arc::new(AdcPausedError));
                    }
                    if self
                        .channels
                        .input_available_at(stamp)
                        .map_err(|error| error as Failure)?
                    {
                        break;
                    }
                    self.wait(None, Duration::from_millis(5))?;
                    stamp = self.snapshot()?["timestamp_ns"].as_u64().unwrap();
                }
                self.channels
                    .send_adc(channel, value, stamp)
                    .map_err(|error| error as Failure)?;
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
    let image = super::firmware_image::read_snapshot(&options.firmware)?;
    with_firmware_image(options, image, consumer)
}

/// The image is held through peer/process cleanup. Watch may hand in its
/// admitted candidate; ordinary consumers acquire the same snapshot above.
pub(super) fn with_firmware_image<T>(
    mut options: LabOptions,
    image: tempfile::NamedTempFile,
    consumer: impl FnOnce(&mut ConnectedLab<'_>) -> Result<T>,
) -> std::result::Result<T, LabError> {
    options.firmware = image.path().to_owned();
    let mut cleanup_failed = false;
    let (firmware, qemu) = options.admit()?;
    let usb_listener = options
        .usbip
        .map(crate::usbip::listen)
        .transpose()
        .map_err(|e| LabError::from(lower(e)))?;
    let streams: Vec<_> = options
        .uarts
        .iter()
        .chain(&options.cans)
        .cloned()
        .chain((options.adc || options.imu != ImuMode::Off).then_some("cosim".into()))
        .chain(options.usbip.map(|_| "usb".into()))
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
    let mut owner = NativeProcess::start_cancellable(
        ProcessOptions {
            argv,
            sockets: std::iter::once("qmp".into())
                .chain(streams.clone())
                .collect(),
            wait_sockets: None,
            startup_timeout: options.startup_timeout,
            host_deadline,
        },
        options.cancellation.as_deref(),
    )
    .map_err(|error| LabError {
        notes: error.notes(),
        cleanup_failed: error.cleanup_failed(),
        primary: error,
    })?;
    let startup_deadline = owner.started() + options.startup_timeout;
    let mut qmp = None;
    let mut uarts = Vec::new();
    let mut cans = Vec::new();
    let mut cosim = None;
    let mut usb_stream = None;
    let mut notes = Vec::new();
    let execution = (|| -> Result<T> {
        owner
            .check_with_deadline(startup_deadline.min(host_deadline))
            .map_err(|error| error as Failure)?;
        #[cfg(unix)]
        let connection = runtime.block_on(startup_operation(
            async {
                QmpSession::connect_uds(
                    owner.socket("qmp").unwrap(),
                    startup_deadline.min(host_deadline),
                )
                .await
                .map_err(|error| error as Failure)
            },
            options.cancellation.as_deref(),
        ))?;
        #[cfg(windows)]
        let connection = {
            let stream = owner.take_stream("qmp").map_err(|error| error as Failure)?;
            runtime.block_on(startup_operation(
                async {
                    QmpSession::from_tcp(stream, startup_deadline.min(host_deadline))
                        .await
                        .map_err(|error| error as Failure)
                },
                options.cancellation.as_deref(),
            ))?
        };
        qmp = Some(connection);
        for name in &streams {
            owner
                .check_with_deadline(startup_deadline.min(host_deadline))
                .map_err(|error| error as Failure)?;
            #[cfg(unix)]
            let deadline = startup_deadline
                .min(host_deadline)
                .min(Instant::now() + Duration::from_secs(2));
            #[cfg(unix)]
            let stream = runtime
                .block_on(startup_operation(
                    async {
                        tokio::time::timeout_at(
                            deadline.into(),
                            tokio::net::UnixStream::connect(owner.socket(name).unwrap()),
                        )
                        .await
                        .map_err(|_| {
                            Arc::new(Error::Deadline("Lab stream connection expired".into()))
                                as Failure
                        })?
                        .map_err(lower)
                    },
                    options.cancellation.as_deref(),
                ))?
                .into_std()
                .map_err(lower)?;
            #[cfg(windows)]
            let stream = owner.take_stream(name).map_err(|error| error as Failure)?;
            if name == "usb" {
                usb_stream = Some(StreamTransport::new(stream, 128).map_err(|e| e as Failure)?);
            } else if name == "cosim" {
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
            adc: options.adc,
            imu: options.imu,
            gdb_enabled: options.gdb_port.is_some(),
            auto_user_release_ns: None,
            usb: usb_stream
                .as_mut()
                .zip(usb_listener)
                .map(|(stream, listener)| crate::usbip::Exporter::new(stream, listener)),
        };
        while !lab
            .channels
            // Windows QEMU connects before its initial machine reset; Unix
            // peers connect after startup. Both use the same wire parser.
            .consume_opening(cfg!(windows))
            .map_err(|error| error as Failure)?
        {
            lab.wait(None, Duration::from_millis(5))?;
        }
        let stamp = lab.snapshot()?["timestamp_ns"].as_u64().unwrap();
        lab.channels
            .reset(stamp)
            .map_err(|error| error as Failure)?;
        lab.startup = false;
        let result = consumer(&mut lab).and_then(|value| {
            lab.check()?;
            Ok(value)
        });
        // USB importer commands have their own cleanup deadlines. End the
        // native active budget before waiting for OS detach.
        let active = lab.owner.begin_cleanup().map_err(|error| error as Failure);
        let result = result.and_then(|value| active.map(|()| value));
        let closed = lab
            .usb
            .as_mut()
            .map(|usb| usb.close())
            .transpose()
            .map_err(lower);
        cleanup_failed |= closed.is_err();
        match (result, closed) {
            (Err(primary), Err(cleanup)) => {
                notes.push(format!("USB importer cleanup: {cleanup}"));
                Err(primary)
            }
            (Err(primary), _) => Err(primary),
            (_, Err(cleanup)) => Err(cleanup),
            (Ok(value), _) => Ok(value),
        }
    })();
    let (value, mut primary) = match execution {
        Ok(value) => (Some(value), None),
        Err(error) => (None, Some(error)),
    };
    if let Err(error) = owner.begin_cleanup() {
        cleanup_error(&mut primary, &mut notes, "Lab final active check", error);
    }
    if let Some(qmp) = &mut qmp {
        if let Err(error) = runtime.block_on(qmp.close()) {
            cleanup_failed |= error.cleanup_failed();
            notes.extend(error.notes());
            cleanup_error(&mut primary, &mut notes, "Lab QMP cleanup", error);
        }
    }
    drop(qmp);
    drop(usb_stream);
    drop(cosim);
    drop(cans);
    drop(uarts);
    if let Err(error) = owner.close() {
        cleanup_failed |= error.cleanup_failed();
        notes.extend(error.notes());
        cleanup_error(&mut primary, &mut notes, "Lab native cleanup", error);
    }
    if let Some(primary) = primary {
        Err(LabError {
            primary,
            notes,
            cleanup_failed,
        })
    } else {
        Ok(value.expect("successful consumer result"))
    }
}
