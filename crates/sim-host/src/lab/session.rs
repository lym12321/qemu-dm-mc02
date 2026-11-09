//! Persistent single-threaded native Lab owner for asynchronous consumers.
//! HTTP/UI callers exchange immutable values; they never borrow native peers.
use super::{
    channels::{AdcValue, ChannelData, ChannelEvent},
    with_lab, AdcPausedError, ConnectedLab, LabError, LabInput, LabOptions,
};
use crate::{boards, can::CanFrame, launch, qmp::commands};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use futures::{
    channel::oneshot,
    future::{FutureExt, Shared},
};
use serde_json::{json, Value};
use sim_core::{Error, Failure};
use std::{
    collections::VecDeque,
    fmt,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender, TrySendError},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use uuid::Uuid;

const COMMAND_LIMIT: usize = 64;
const HISTORY_LIMIT: usize = 4096;
const HISTORY_BYTES: usize = 1024 * 1024;

#[derive(Clone, Debug)]
pub struct SessionError {
    pub primary: Failure,
    pub notes: Vec<String>,
    pub input: bool,
}
impl fmt::Display for SessionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.primary.fmt(f)
    }
}
impl std::error::Error for SessionError {}
type Result<T> = std::result::Result<T, SessionError>;
pub type CommandReply = oneshot::Receiver<Result<Value>>;
pub type StopReply = Shared<CommandReply>;
fn refused(message: impl Into<String>) -> SessionError {
    SessionError {
        primary: Arc::new(Error::Admission(message.into())),
        notes: vec![],
        input: true,
    }
}
fn failed(primary: Failure) -> SessionError {
    SessionError {
        primary,
        notes: vec![],
        input: false,
    }
}
fn active(status: &str) -> bool {
    matches!(status, "starting" | "paused" | "running" | "stopping")
}

#[derive(Clone, Debug)]
pub struct SessionOptions {
    pub firmware: Option<PathBuf>,
    pub qemu: Option<PathBuf>,
    pub timeout: Duration,
    pub startup_timeout: Duration,
    pub qmp_path: Option<PathBuf>,
    pub gdb_port: Option<u16>,
}
impl Default for SessionOptions {
    fn default() -> Self {
        Self {
            firmware: None,
            qemu: None,
            timeout: Duration::from_secs(3600),
            startup_timeout: Duration::from_secs(10),
            qmp_path: None,
            gdb_port: None,
        }
    }
}
impl SessionOptions {
    fn admit(mut self) -> Result<Self> {
        if self.startup_timeout.is_zero()
            || self.timeout < self.startup_timeout
            || Instant::now().checked_add(self.timeout).is_none()
        {
            return Err(refused(
                "startup timeout must be positive and not exceed total host timeout",
            ));
        }
        if self.gdb_port == Some(0) {
            return Err(refused("GDB port must be in 1..65535"));
        }
        self.qemu = Some(launch::resolve_qemu(self.qemu).map_err(|e| refused(e.to_string()))?);
        self.qmp_path =
            launch::socket_path(self.qmp_path.as_deref()).map_err(|e| refused(e.to_string()))?;
        self.firmware = self
            .firmware
            .map(|path| {
                launch::readable_file(&path, "firmware").map_err(|e| refused(e.to_string()))
            })
            .transpose()?;
        Ok(self)
    }
    fn lab(&self, firmware: PathBuf) -> LabOptions {
        LabOptions {
            firmware,
            qemu: self.qemu.clone(),
            uarts: boards::launch::UARTS
                .iter()
                .map(|name| (*name).into())
                .collect(),
            cans: boards::launch::CANS
                .iter()
                .map(|name| (*name).into())
                .collect(),
            adc: true,
            startup_timeout: self.startup_timeout,
            timeout: self.timeout,
            qmp_path: self.qmp_path.clone(),
            gdb_port: self.gdb_port,
            cancellation: None,
        }
    }
}

#[derive(Debug)]
enum Action {
    Load,
    Run,
    Pause,
    Vin(u32),
    Input(LabInput),
}
fn fields<'a>(value: &'a Value, expected: &[&str]) -> Result<&'a serde_json::Map<String, Value>> {
    value
        .as_object()
        .filter(|map| {
            map.len() == expected.len() && expected.iter().all(|key| map.contains_key(*key))
        })
        .ok_or_else(|| refused(format!("expected fields: {}", expected.join(", "))))
}
fn uint(value: &Value, name: &str, maximum: u64) -> Result<u64> {
    value
        .as_u64()
        .filter(|value| *value <= maximum)
        .ok_or_else(|| refused(format!("{name} must be an integer in 0..{maximum}")))
}
fn text<'a>(value: &'a Value, label: &str) -> Result<&'a str> {
    value
        .as_str()
        .ok_or_else(|| refused(format!("{label} must be a string")))
}
fn admit_action(action: &str, args: &Value) -> Result<Action> {
    Ok(match action {
        "load" | "run" | "pause" => {
            fields(args, &[])?;
            match action {
                "load" => Action::Load,
                "run" => Action::Run,
                _ => Action::Pause,
            }
        }
        "key" => {
            fields(args, &["pressed"])?;
            Action::Input(LabInput::Key(
                args["pressed"]
                    .as_bool()
                    .ok_or_else(|| refused("pressed must be a boolean"))?,
            ))
        }
        "gpio" => {
            fields(args, &["pin", "level"])?;
            let pin = text(&args["pin"], "pin")?;
            boards::gpio_pin(pin).map_err(|e| refused(e.to_string()))?;
            Action::Input(LabInput::Gpio {
                pin: pin.into(),
                level: uint(&args["level"], "level", 1)? != 0,
            })
        }
        "vin" => {
            fields(args, &["millivolts"])?;
            Action::Vin(uint(&args["millivolts"], "millivolts", u32::MAX.into())? as u32)
        }
        "uart" => {
            fields(args, &["endpoint", "data_b64"])?;
            let endpoint = text(&args["endpoint"], "endpoint")?;
            if !boards::launch::UARTS.contains(&endpoint) {
                return Err(refused("unknown UART endpoint"));
            }
            let encoded = text(&args["data_b64"], "data_b64")?;
            if encoded.len() > 5464 {
                return Err(refused("UART input must be at most 4096 bytes"));
            }
            let data = STANDARD
                .decode(encoded)
                .map_err(|e| refused(e.to_string()))?;
            if data.len() > 4096 {
                return Err(refused("UART input must be at most 4096 bytes"));
            }
            Action::Input(LabInput::Uart {
                endpoint: endpoint.into(),
                data,
            })
        }
        "can" => {
            fields(args, &["endpoint", "can_id", "flags", "dlc", "data_hex"])?;
            let endpoint = text(&args["endpoint"], "endpoint")?;
            if !boards::launch::CANS.contains(&endpoint) {
                return Err(refused("unknown CAN endpoint"));
            }
            let encoded = text(&args["data_hex"], "data_hex")?;
            if encoded.len() > 192 {
                return Err(refused("CAN payload must be at most 64 bytes"));
            }
            let mut bytes = encoded.bytes();
            let mut data = Vec::new();
            while let Some(first) = bytes.next() {
                if first.is_ascii_whitespace() {
                    continue;
                }
                let second = bytes
                    .next()
                    .ok_or_else(|| refused("CAN hex payload requires complete byte pairs"))?;
                let digit = |v: u8| {
                    (v as char)
                        .to_digit(16)
                        .ok_or_else(|| refused("invalid CAN hex payload"))
                };
                data.push((digit(first)? * 16 + digit(second)?) as u8);
            }
            let frame = CanFrame::new(
                uint(&args["can_id"], "can_id", 0x1fffffff)? as u32,
                uint(&args["flags"], "flags", 15)? as u32,
                Some(uint(&args["dlc"], "dlc", 15)? as u8),
                data,
                0,
            )
            .map_err(|e| refused(e.to_string()))?;
            Action::Input(LabInput::Can {
                endpoint: endpoint.into(),
                frame,
            })
        }
        "adc" => {
            let map = args
                .as_object()
                .ok_or_else(|| refused("ADC args must be an object"))?;
            let (field, maximum) = if map.contains_key("raw") {
                ("raw", 65535)
            } else {
                ("voltage_uv", 3300000)
            };
            fields(args, &["channel", field])?;
            let channel = uint(&args["channel"], "channel", 31)? as u16;
            let value = uint(&args[field], field, maximum)?;
            Action::Input(LabInput::Adc {
                channel,
                value: if field == "raw" {
                    AdcValue::Raw(value as u16)
                } else {
                    AdcValue::Voltage(value as u32)
                },
            })
        }
        _ => return Err(refused("unknown action")),
    })
}

struct Command {
    action: Action,
    session_id: Option<String>,
    controller: Option<String>,
    epoch: u64,
    reply: oneshot::Sender<Result<Value>>,
}
struct PendingStop {
    sender: oneshot::Sender<Result<Value>>,
    reply: StopReply,
}
struct History {
    items: VecDeque<(u64, Value, usize)>,
    bytes: usize,
    sequence: u64,
    dropped: u64,
    dropped_bytes: u64,
}
impl History {
    fn new() -> Self {
        Self {
            items: VecDeque::new(),
            bytes: 0,
            sequence: 0,
            dropped: 0,
            dropped_bytes: 0,
        }
    }
    fn push(&mut self, mut event: Value, session: &Value) {
        self.sequence += 1;
        event["seq"] = self.sequence.to_string().into();
        event["session_id"] = session.clone();
        let size = serde_json::to_vec(&event)
            .expect("serializable completed event")
            .len();
        self.bytes += size;
        self.items.push_back((self.sequence, event, size));
        while self.items.len() > HISTORY_LIMIT || self.bytes > HISTORY_BYTES {
            let (_, _, removed) = self.items.pop_front().unwrap();
            self.bytes -= removed;
            self.dropped += 1;
            self.dropped_bytes += removed as u64;
        }
    }
    fn after(&self, after: u64, session: &Value) -> Vec<Value> {
        let mut events = Vec::new();
        if let Some((first, _, _)) = self.items.front() {
            if after < first - 1 {
                events.push(json!({"type":"gap", "seq":(first - 1).to_string(),
                    "session_id":session, "count":self.dropped, "bytes":self.dropped_bytes}));
            }
        }
        events.extend(
            self.items
                .iter()
                .filter(|(seq, _, _)| *seq > after)
                .map(|(_, value, _)| value.clone()),
        );
        events
    }
}
struct State {
    value: Value,
    firmware: Option<PathBuf>,
    epoch: u64,
    pending_load: bool,
    key_controller: Option<String>,
    stop: Option<PendingStop>,
    history: History,
    failure: Option<SessionError>,
}
struct SharedState {
    state: Mutex<State>,
    stop: AtomicBool,
    release: AtomicBool,
    closing: AtomicBool,
}
pub struct LabSession {
    shared: Arc<SharedState>,
    queue: SyncSender<Command>,
    owner: Mutex<Option<JoinHandle<()>>>,
    done: Mutex<Receiver<()>>,
    close_timeout: Duration,
}
impl LabSession {
    pub fn new(options: SessionOptions) -> Result<Self> {
        let options = options.admit()?;
        let firmware = options.firmware.clone();
        let firmware_name = firmware
            .as_ref()
            .and_then(|path| path.file_name())
            .map(|name| name.to_string_lossy().into_owned());
        let value = json!({"type":"state", "status":"idle", "session_id":null,
            "owner_id":Uuid::new_v4().simple().to_string(), "firmware":firmware_name.map(|name| json!({"name":name})),
            "board":null,"pins":[],"observation_stale":true,"remaining_seconds":null,
            "gdb_port":options.gdb_port,"error":null,"notes":[]});
        let shared = Arc::new(SharedState {
            state: Mutex::new(State {
                value,
                firmware,
                epoch: 0,
                pending_load: false,
                key_controller: None,
                stop: None,
                history: History::new(),
                failure: None,
            }),
            stop: AtomicBool::new(false),
            release: AtomicBool::new(false),
            closing: AtomicBool::new(false),
        });
        let (queue, commands) = mpsc::sync_channel(COMMAND_LIMIT);
        let (done_tx, done) = mpsc::channel();
        let close_timeout = options
            .startup_timeout
            .saturating_add(Duration::from_secs(35));
        let worker_state = shared.clone();
        let owner = thread::Builder::new()
            .name("lab-session".into())
            .spawn(move || {
                owner_main(&worker_state, &commands, &options);
                let _ = done_tx.send(());
            })
            .map_err(|error| failed(Arc::new(error)))?;
        Ok(Self {
            shared,
            queue,
            owner: Mutex::new(Some(owner)),
            done: Mutex::new(done),
            close_timeout,
        })
    }
    pub fn state(&self) -> Value {
        self.shared.state.lock().unwrap().value.clone()
    }
    pub fn select_firmware(&self, path: PathBuf, name: Option<&str>) -> Result<Value> {
        let path =
            launch::readable_file(&path, "firmware").map_err(|error| refused(error.to_string()))?;
        let mut state = self.shared.state.lock().unwrap();
        if self.shared.closing.load(Ordering::Relaxed)
            || active(state.value["status"].as_str().unwrap())
            || state.pending_load
        {
            return Err(refused(
                "stop the current experiment before replacing firmware",
            ));
        }
        let display = name
            .map(str::to_owned)
            .unwrap_or_else(|| path.file_name().unwrap().to_string_lossy().into_owned());
        state.firmware = Some(path);
        state.value["firmware"] = json!({"name":display});
        Ok(state.value.clone())
    }
    pub fn submit(
        &self,
        action: &str,
        args: Value,
        session_id: Option<&str>,
        controller: Option<&str>,
    ) -> CommandReply {
        let (reply, receiver) = oneshot::channel();
        let action = match admit_action(action, &args) {
            Ok(action) => action,
            Err(error) => {
                let _ = reply.send(Err(error));
                return receiver;
            }
        };
        // One admission lock covers state, queue insertion and the load marker.
        let mut state = self.shared.state.lock().unwrap();
        let status = state.value["status"].as_str().unwrap();
        let loading = matches!(action, Action::Load);
        let refusal = if self.shared.closing.load(Ordering::Relaxed) {
            Some(refused("workbench is closing"))
        } else if session_id != state.value["session_id"].as_str() {
            Some(refused("stale session; refresh state before submitting"))
        } else if loading && (active(status) || state.pending_load) {
            Some(refused("a board session is already active"))
        } else if loading && state.firmware.is_none() {
            Some(refused("select firmware first"))
        } else if !loading && !matches!(status, "starting" | "paused" | "running") {
            Some(refused("load a board session first"))
        } else {
            None
        };
        if let Some(error) = refusal {
            let _ = reply.send(Err(error));
            return receiver;
        }
        let command = Command {
            action,
            session_id: session_id.map(str::to_owned),
            controller: controller.map(str::to_owned),
            epoch: state.epoch,
            reply,
        };
        match self.queue.try_send(command) {
            Ok(()) => {
                if loading {
                    state.pending_load = true;
                }
            }
            Err(TrySendError::Full(command)) => {
                let _ = command.reply.send(Err(refused(
                    "command queue is full; retry after pending actions finish",
                )));
            }
            Err(TrySendError::Disconnected(command)) => {
                let _ = command.reply.send(Err(refused("workbench is closing")));
            }
        }
        receiver
    }
    pub fn request_stop(&self, session_id: Option<&str>) -> StopReply {
        let mut state = self.shared.state.lock().unwrap();
        if session_id.is_some() && session_id != state.value["session_id"].as_str() {
            let (tx, rx) = oneshot::channel();
            let _ = tx.send(Err(refused("stale session")));
            return rx.shared();
        }
        state.epoch += 1;
        self.shared.stop.store(true, Ordering::Relaxed);
        if state.stop.is_none() {
            let (sender, receiver) = oneshot::channel();
            state.stop = Some(PendingStop {
                sender,
                reply: receiver.shared(),
            });
        }
        state.stop.as_ref().unwrap().reply.clone()
    }
    pub fn disconnect(&self, controller: &str) {
        let mut state = self.shared.state.lock().unwrap();
        state.epoch += 1;
        if state.key_controller.as_deref() == Some(controller) {
            self.shared.release.store(true, Ordering::Relaxed);
        }
    }
    pub fn history(&self, after: u64) -> Vec<Value> {
        let state = self.shared.state.lock().unwrap();
        state.history.after(after, &state.value["session_id"])
    }
    pub fn close(&self) -> Result<()> {
        self.shared.closing.store(true, Ordering::Relaxed);
        drop(self.request_stop(None));
        let mut owner = self.owner.lock().unwrap();
        if owner.is_some() {
            self.done
                .lock()
                .unwrap()
                .recv_timeout(self.close_timeout)
                .map_err(|_| {
                    failed(Arc::new(Error::State(
                        "Web Lab owner did not finish bounded cleanup".into(),
                    )))
                })?;
            owner.take().unwrap().join().map_err(|_| {
                failed(Arc::new(Error::State(
                    "Web Lab owner exited without completing cleanup".into(),
                )))
            })?;
        }
        self.shared
            .state
            .lock()
            .unwrap()
            .failure
            .clone()
            .map_or(Ok(()), Err)
    }
}
impl Drop for LabSession {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

fn cancel_pending(commands: &Receiver<Command>) {
    while let Ok(command) = commands.try_recv() {
        let _ = command
            .reply
            .send(Err(refused("session ended before request was applied")));
    }
}
fn next_command(shared: &SharedState, commands: &Receiver<Command>) -> Option<Command> {
    let command = commands.try_recv().ok()?;
    if command.reply.is_canceled() {
        return None;
    }
    let admissible = {
        let state = shared.state.lock().unwrap();
        if command.epoch != state.epoch {
            Err(refused(
                "request cancelled after stop or controller disconnect",
            ))
        } else if command.session_id.as_deref() != state.value["session_id"].as_str() {
            Err(refused("stale session"))
        } else {
            Ok(())
        }
    };
    match admissible {
        Ok(()) => Some(command),
        Err(error) => {
            let _ = command.reply.send(Err(error));
            None
        }
    }
}
fn key_applied(shared: &SharedState, controller: Option<String>, epoch: u64, pressed: bool) {
    let mut state = shared.state.lock().unwrap();
    state.key_controller = if pressed { controller } else { None };
    if epoch != state.epoch {
        shared.release.store(true, Ordering::Relaxed);
    }
}
fn finish_stop(shared: &SharedState, result: Result<Value>) {
    let pending = {
        let mut state = shared.state.lock().unwrap();
        shared.stop.store(false, Ordering::Relaxed);
        state.stop.take()
    };
    if let Some(pending) = pending {
        let _ = pending.sender.send(result);
    }
}
fn owner_main(shared: &SharedState, commands: &Receiver<Command>, options: &SessionOptions) {
    while !shared.closing.load(Ordering::Relaxed) {
        if shared.stop.load(Ordering::Relaxed) {
            cancel_pending(commands);
            shared.state.lock().unwrap().pending_load = false;
            finish_stop(shared, Ok(json!({"accepted":true})));
        }
        let command = match commands.recv_timeout(Duration::from_millis(20)) {
            Ok(command) => command,
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        };
        if !matches!(command.action, Action::Load) {
            let _ = command
                .reply
                .send(Err(refused("load a board session first")));
            continue;
        }
        if command.reply.is_canceled() {
            shared.state.lock().unwrap().pending_load = false;
            continue;
        }
        run_session(shared, commands, options, command);
    }
    cancel_pending(commands);
    finish_stop(shared, Ok(json!({"accepted":true})));
}
fn emit(shared: &SharedState, events: Vec<ChannelEvent>) {
    let mut state = shared.state.lock().unwrap();
    let session = state.value["session_id"].clone();
    for event in events {
        let value = match event.data {
            ChannelData::Uart(data) => json!({"type":"uart","endpoint":event.endpoint,
                "timestamp_ns":event.timestamp_ns.to_string(),"data_b64":STANDARD.encode(data)}),
            ChannelData::Can(frame) => json!({"type":"can","endpoint":event.endpoint,
                "timestamp_ns":event.timestamp_ns.to_string(),"can_id":frame.can_id(),
                "flags":frame.flags(),"dlc":frame.dlc(),
                "data_hex":frame.data().iter().map(|byte| format!("{byte:02x}")).collect::<String>()}),
        };
        state.history.push(value, &session);
    }
}
fn project(mut snapshot: Value) -> sim_core::Result<(Value, Value)> {
    let mut pins = Vec::with_capacity(128);
    for port in b'A'..=b'H' {
        let bank = &snapshot["gpio"][char::from(port).to_string()];
        for bit in 0..16 {
            let pin = format!("{}{bit}", char::from(port));
            pins.push(
                json!({"pin":pin,"mode":(bank["moder"].as_u64().unwrap() >> (bit*2)) & 3,
                "level":u8::from(boards::digital_level(&format!("gpio.{pin}"), &snapshot)?),
                "odr":(bank["odr"].as_u64().unwrap() >> bit) & 1,
                "output":bank["output_mask"].as_u64().unwrap() & (1 << bit) != 0}),
            );
        }
    }
    snapshot["timestamp_ns"] = snapshot["timestamp_ns"]
        .as_u64()
        .unwrap()
        .to_string()
        .into();
    Ok((snapshot, pins.into()))
}
fn observe(
    shared: &SharedState,
    lab: &mut ConnectedLab<'_>,
    snapshot: Value,
) -> sim_core::Result<()> {
    let running = lab.command(commands::query_status {})?.running;
    let remaining = lab.remaining()?.as_secs_f64();
    let (board, pins) = project(snapshot)?;
    let mut state = shared.state.lock().unwrap();
    state.value["status"] = if running { "running" } else { "paused" }.into();
    state.value["board"] = board;
    state.value["pins"] = pins;
    state.value["remaining_seconds"] = json!(remaining);
    state.value["observation_stale"] = false.into();
    Ok(())
}
fn run_session(
    shared: &SharedState,
    commands: &Receiver<Command>,
    options: &SessionOptions,
    command: Command,
) {
    let firmware = {
        let mut state = shared.state.lock().unwrap();
        state.pending_load = false;
        if command.epoch != state.epoch {
            let _ = command.reply.send(Err(refused("load cancelled")));
            return;
        }
        state.value["status"] = "starting".into();
        state.value["session_id"] = Uuid::new_v4().simple().to_string().into();
        state.value["error"] = Value::Null;
        state.value["notes"] = json!([]);
        state.failure = None;
        state.value["board"] = Value::Null;
        state.value["pins"] = json!([]);
        state.value["observation_stale"] = true.into();
        state.firmware.clone().unwrap()
    };
    let mut initial_reply = Some(command.reply);
    let result = with_lab(options.lab(firmware), |lab| {
        let (snapshot, events) = lab.pump()?;
        emit(shared, events);
        observe(shared, lab, snapshot)?;
        let cancelled =
            shared.stop.load(Ordering::Relaxed) || shared.closing.load(Ordering::Relaxed);
        let _ = initial_reply.take().unwrap().send(if cancelled {
            Err(refused("load cancelled"))
        } else {
            Ok(json!({"accepted":true}))
        });
        let mut next_observation = Instant::now() + Duration::from_millis(100);
        while !shared.stop.load(Ordering::Relaxed) && !shared.closing.load(Ordering::Relaxed) {
            if shared.release.swap(false, Ordering::Relaxed) {
                lab.inject(LabInput::Key(false))?;
                shared.state.lock().unwrap().key_controller = None;
            }
            let current = next_command(shared, commands);
            let had_command = current.is_some();
            if let Some(current) = current {
                let action = match current.action {
                    Action::Load => Err(Arc::new(Error::Admission(
                        "a board session is already active".into(),
                    )) as Failure),
                    Action::Run => lab.resume(),
                    Action::Pause => lab.pause(),
                    Action::Vin(value) => lab.set_vin(value),
                    Action::Input(input) => {
                        let pressed = if let LabInput::Key(value) = &input {
                            Some(*value)
                        } else {
                            None
                        };
                        lab.inject(input).map(|_| {
                            if let Some(pressed) = pressed {
                                key_applied(shared, current.controller, current.epoch, pressed);
                            }
                        })
                    }
                };
                match action {
                    Ok(()) => {
                        let _ = current.reply.send(Ok(json!({"accepted":true})));
                    }
                    Err(error) => {
                        let retryable = error.downcast_ref::<AdcPausedError>().is_some()
                            || matches!(
                                error.downcast_ref::<crate::Error>(),
                                Some(crate::Error::Capacity(_))
                            );
                        let _ = current.reply.send(Err(if retryable {
                            refused(error.to_string())
                        } else {
                            failed(error.clone())
                        }));
                        if !retryable {
                            return Err(error);
                        }
                    }
                }
            }
            let (snapshot, events) = lab.pump()?;
            emit(shared, events);
            if had_command || Instant::now() >= next_observation {
                observe(shared, lab, snapshot)?;
                next_observation = Instant::now() + Duration::from_millis(100);
            }
            lab.wait(None, Duration::from_millis(5))?;
        }
        shared.state.lock().unwrap().value["status"] = "stopping".into();
        Ok(())
    });
    let completion = match result {
        Ok(()) => {
            let mut state = shared.state.lock().unwrap();
            state.value["status"] = "stopped".into();
            state.value["observation_stale"] = true.into();
            state.value["remaining_seconds"] = Value::Null;
            Ok(json!({"accepted":true}))
        }
        Err(LabError { primary, notes }) => {
            let error = SessionError {
                primary,
                notes,
                input: false,
            };
            if let Some(sender) = initial_reply.take() {
                let _ = sender.send(Err(error.clone()));
            }
            let mut state = shared.state.lock().unwrap();
            state.value["status"] = "failed".into();
            state.value["error"] = error.to_string().into();
            state.value["notes"] = json!(error.notes);
            state.value["observation_stale"] = true.into();
            state.value["remaining_seconds"] = Value::Null;
            state.failure = Some(error.clone());
            Err(error)
        }
    };
    {
        let mut state = shared.state.lock().unwrap();
        state.pending_load = false;
        state.key_controller = None;
    }
    shared.release.store(false, Ordering::Relaxed);
    cancel_pending(commands);
    finish_stop(shared, completion);
}

#[cfg(test)]
mod tests {
    use super::*;
    fn paused_session(capacity: usize) -> (LabSession, Receiver<Command>) {
        let shared = Arc::new(SharedState {
            state: Mutex::new(State {
                value: json!({"status":"paused","session_id":"current"}),
                firmware: None,
                epoch: 0,
                pending_load: false,
                key_controller: None,
                stop: None,
                history: History::new(),
                failure: None,
            }),
            stop: AtomicBool::new(false),
            release: AtomicBool::new(false),
            closing: AtomicBool::new(false),
        });
        let (queue, commands) = mpsc::sync_channel(capacity);
        let (_, done) = mpsc::channel();
        (
            LabSession {
                shared,
                queue,
                owner: Mutex::new(None),
                done: Mutex::new(done),
                close_timeout: Duration::ZERO,
            },
            commands,
        )
    }
    #[test]
    fn pure_admission_keeps_bytes_and_rejects_bool_fields() {
        let Action::Input(LabInput::Uart { data, .. }) =
            admit_action("uart", &json!({"endpoint":"uart1","data_b64":"AP8="})).unwrap()
        else {
            panic!()
        };
        assert_eq!(data, [0, 255]);
        for (action, value) in [
            ("key", json!({"pressed":1})),
            ("gpio", json!({"pin":"B0","level":true})),
            ("adc", json!({"channel":0,"raw":1,"voltage_uv":2})),
            (
                "can",
                json!({"endpoint":"can1","can_id":1,"flags":6,"dlc":0,"data_hex":""}),
            ),
        ] {
            assert!(admit_action(action, &value).unwrap_err().input);
        }
    }
    #[test]
    fn bounded_history_keeps_exact_strings_and_reports_byte_gap() {
        let mut history = History::new();
        history.push(
            json!({"type":"uart","timestamp_ns":((1u64<<53)+17).to_string(),"data_b64":"AP8="}),
            &json!("first"),
        );
        let first = history.after(0, &json!("first"));
        assert_eq!(first[0]["timestamp_ns"], "9007199254741009");
        for _ in 0..300 {
            history.push(
                json!({"type":"uart","data_b64":"x".repeat(5464)}),
                &json!("next"),
            );
        }
        let retained = history.after(0, &json!("next"));
        assert_eq!(retained[0]["type"], "gap");
        assert!(retained[0]["bytes"].as_u64().unwrap() > 0);
        assert!(
            retained[1..]
                .iter()
                .map(|value| serde_json::to_vec(value).unwrap().len())
                .sum::<usize>()
                <= HISTORY_BYTES
        );
        assert!(history.after(301, &Value::Null).is_empty());
    }
    #[test]
    fn projection_uses_sampled_levels_and_preserves_large_timestamp() {
        let mut gpio = serde_json::Map::new();
        for port in b'A'..=b'H' {
            gpio.insert(
                char::from(port).to_string(),
                json!({"moder":0,"sampled":0,"odr":0,"output_mask":0}),
            );
        }
        gpio.insert(
            "B".into(),
            json!({"moder":9,"sampled":1,"odr":2,"output_mask":1}),
        );
        let (board, pins) = project(json!({"schema":1,"timestamp_ns":9007199254741009u64,
            "board":"dm-mc02","vin_mv":24000,"gpio":gpio,
            "key":{"pin":"A15","pressed":false,"level":0},
            "led":{"pin":"A7","rgb":0,"brightness":0,"source":"gpio-latch"}}))
        .unwrap();
        assert_eq!(board["timestamp_ns"], "9007199254741009");
        assert_eq!(
            pins[16],
            json!({"pin":"B0","mode":1,"level":1,"odr":0,"output":true})
        );
        assert_eq!(
            pins[17],
            json!({"pin":"B1","mode":2,"level":0,"odr":1,"output":false})
        );
    }
    #[test]
    fn stop_bypasses_a_full_command_queue_and_waits_for_owner_completion() {
        // Isolate only native acquisition: use the real admission, bounded queue,
        // receiver cancellation and stop completion methods without an MCU.
        let (session, commands) = paused_session(1);
        let first = session.submit(
            "vin",
            json!({"millivolts":12000}),
            Some("current"),
            Some("page"),
        );
        let full = futures::executor::block_on(session.submit(
            "vin",
            json!({"millivolts":11000}),
            Some("current"),
            Some("page"),
        ))
        .unwrap()
        .unwrap_err();
        assert!(full.input && full.to_string().contains("queue is full"));
        drop(session.request_stop(Some("current")));
        let stopped = session.request_stop(Some("current"));
        assert!(stopped.clone().now_or_never().is_none());
        cancel_pending(&commands);
        assert!(
            futures::executor::block_on(first)
                .unwrap()
                .unwrap_err()
                .input
        );
        finish_stop(&session.shared, Ok(json!({"accepted":true})));
        assert_eq!(
            futures::executor::block_on(stopped).unwrap().unwrap(),
            json!({"accepted":true})
        );
    }
    #[test]
    fn cancelled_and_disconnected_requests_do_not_reach_native_dispatch() {
        let (session, commands) = paused_session(3);
        drop(session.submit(
            "vin",
            json!({"millivolts":12000}),
            Some("current"),
            Some("page"),
        ));
        let disconnected = session.submit(
            "key",
            json!({"pressed":true}),
            Some("current"),
            Some("page"),
        );
        session.disconnect("page");
        let live = session.submit(
            "vin",
            json!({"millivolts":11000}),
            Some("current"),
            Some("next-page"),
        );
        assert!(next_command(&session.shared, &commands).is_none());
        assert!(next_command(&session.shared, &commands).is_none());
        let error = futures::executor::block_on(disconnected)
            .unwrap()
            .unwrap_err();
        assert!(error.input && error.to_string().contains("disconnect"));
        // A current request still advances after skipped queue entries.
        let command = next_command(&session.shared, &commands).unwrap();
        command.reply.send(Ok(json!({"accepted":true}))).unwrap();
        assert_eq!(
            futures::executor::block_on(live).unwrap().unwrap(),
            json!({"accepted":true})
        );
    }
    #[test]
    fn disconnect_during_native_key_operation_releases_the_completed_press() {
        let (session, commands) = paused_session(1);
        let reply = session.submit(
            "key",
            json!({"pressed":true}),
            Some("current"),
            Some("page"),
        );
        let command = next_command(&session.shared, &commands).unwrap();
        session.disconnect("page");
        // No press was committed when disconnect ran. Completion must still
        // notify the owner's next release turn, even if its waiter vanished.
        assert!(!session.shared.release.swap(false, Ordering::Relaxed));
        drop(reply);
        key_applied(&session.shared, command.controller, command.epoch, true);
        assert!(session.shared.release.swap(false, Ordering::Relaxed));
    }
}
