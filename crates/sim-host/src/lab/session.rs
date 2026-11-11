//! Persistent single-threaded native Lab owner for asynchronous consumers.
//! HTTP/UI callers exchange immutable values; they never borrow native peers.
use super::{
    AdcPausedError, BoardCommand, ConnectedLab, LabError, LabInput,
    channels::ChannelEvent,
    firmware_image::{FirmwareWatch, read_snapshot},
    project::{LabProject, ProjectFile},
};
use crate::{launch, qmp::commands};
use futures::{
    channel::oneshot,
    future::{FutureExt, Shared},
};
use serde_json::{Value, json};
use sim_core::{Error, Failure};
use std::{
    collections::VecDeque,
    fmt,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender, TrySendError},
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
    pub usbip: Option<std::net::SocketAddr>,
    pub project: Option<Arc<ProjectFile>>,
    pub launch: Option<LabProject>,
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
            usbip: None,
            project: None,
            launch: None,
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
}

#[derive(Debug)]
enum Action {
    Load {
        config: LabProject,
        start: bool,
        image: Option<tempfile::NamedTempFile>,
    },
    Board(BoardCommand),
}
fn admit_action(action: &str, args: &Value) -> Result<Action> {
    super::presentation::decode(action, args)
        .map(Action::Board)
        .map_err(refused)
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
    config: LabProject,
    epoch: u64,
    pending_load: bool,
    key_controller: Option<String>,
    stop: Option<PendingStop>,
    history: History,
    failure: Option<SessionError>,
    cleanup_failed: bool,
}
struct SharedState {
    state: Mutex<State>,
    stop: Arc<AtomicBool>,
    release: AtomicBool,
    closing: AtomicBool,
}
pub struct LabSession {
    shared: Arc<SharedState>,
    queue: SyncSender<Command>,
    owner: Mutex<Option<JoinHandle<()>>>,
    done: Mutex<Receiver<()>>,
    close_timeout: Mutex<Duration>,
    project_file: Option<Arc<ProjectFile>>,
    project_config: Mutex<LabProject>,
}
impl LabSession {
    pub fn new(options: SessionOptions) -> Result<Self> {
        let options = options.admit()?;
        let mut config = options.launch.clone().unwrap_or_else(|| LabProject {
            firmware: options.firmware.clone(),
            startup_timeout_secs: options.startup_timeout.as_secs_f64(),
            timeout_secs: options.timeout.as_secs_f64(),
            gdb_port: options.gdb_port,
            usbip: options.usbip,
            ..LabProject::default()
        });
        config.validate().map_err(|e| refused(e.to_string()))?;
        if let Some(file) = &options.project {
            config = file.resolve(&config).map_err(|e| refused(e.to_string()))?;
        }
        let firmware = config.firmware.clone();
        let project_file = options.project.clone();
        let project_config = Mutex::new(config.clone());
        let firmware_name = firmware
            .as_ref()
            .and_then(|path| path.file_name())
            .map(|name| name.to_string_lossy().into_owned());
        let value = json!({"type":"state", "status":"idle", "session_id":null,
            "owner_id":Uuid::new_v4().simple().to_string(), "firmware":firmware_name.map(|name| json!({"name":name})),
            "board":null,"pins":[],"observation_stale":true,"remaining_seconds":null,
            "gdb_port":config.gdb_port,"error":null,"notes":[]});
        let shared = Arc::new(SharedState {
            state: Mutex::new(State {
                value,
                firmware,
                config,
                epoch: 0,
                pending_load: false,
                key_controller: None,
                stop: None,
                history: History::new(),
                failure: None,
                cleanup_failed: false,
            }),
            stop: Arc::new(AtomicBool::new(false)),
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
            close_timeout: Mutex::new(close_timeout),
            project_file,
            project_config,
        })
    }
    pub fn state(&self) -> Value {
        self.shared.state.lock().unwrap().value.clone()
    }
    pub fn project_info(&self) -> Value {
        json!({"path": self.project_file.as_ref().map(|file| file.path()),
            "project": *self.project_config.lock().unwrap()})
    }
    pub fn ide_config(&self, value: Value) -> Result<Value> {
        let mut config: LabProject =
            serde_json::from_value(value).map_err(|e| refused(e.to_string()))?;
        if let Some(file) = &self.project_file {
            config = file.resolve(&config).map_err(|e| refused(e.to_string()))?;
        }
        config.ide_config().map_err(|e| refused(e.to_string()))
    }
    pub fn save_project(&self, value: Value) -> Result<Value> {
        let config: LabProject =
            serde_json::from_value(value).map_err(|e| refused(e.to_string()))?;
        let file = self
            .project_file
            .as_ref()
            .ok_or_else(|| refused("start with --project to enable saving"))?;
        file.save(&config).map_err(|e| refused(e.to_string()))?;
        *self.project_config.lock().unwrap() = config;
        Ok(self.project_info())
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
        let mut config = self.project_config.lock().unwrap();
        config.firmware = None;
        config.watch_elf = false;
        state.value["firmware"] = json!({"name":display});
        Ok(state.value.clone())
    }
    fn admit_launch(&self, args: &Value, selected: Option<&PathBuf>) -> Result<LabProject> {
        let mut config = if args.as_object().is_some_and(|m| m.is_empty()) {
            self.project_config.lock().unwrap().clone()
        } else {
            let map = args
                .as_object()
                .filter(|m| m.len() == 1 && m.contains_key("config"))
                .ok_or_else(|| refused("expected config"))?;
            serde_json::from_value(map["config"].clone()).map_err(|e| refused(e.to_string()))?
        };
        if let Some(file) = &self.project_file {
            config = file.resolve(&config).map_err(|e| refused(e.to_string()))?;
        }
        if config.firmware.is_none() {
            config.firmware = selected.cloned();
        }
        config.validate().map_err(|e| refused(e.to_string()))?;
        let path = config
            .firmware
            .as_ref()
            .ok_or_else(|| refused("select firmware first"))?;
        config.firmware =
            Some(launch::readable_file(path, "firmware ELF").map_err(|e| refused(e.to_string()))?);
        Ok(config)
    }
    pub fn submit(
        &self,
        action: &str,
        args: Value,
        session_id: Option<&str>,
        controller: Option<&str>,
    ) -> CommandReply {
        let state = self.shared.state.lock().unwrap();
        let epoch = state.epoch;
        let action = (|| {
            if matches!(action, "load" | "start") {
                let config = self.admit_launch(&args, state.firmware.as_ref())?;
                Ok(Action::Load {
                    config,
                    start: action == "start",
                    image: None,
                })
            } else if action == "action" {
                let map = args
                    .as_object()
                    .filter(|m| m.len() == 1 && m.contains_key("name"))
                    .ok_or_else(|| refused("expected action name"))?;
                let name = map["name"]
                    .as_str()
                    .ok_or_else(|| refused("action name must be a string"))?;
                let shortcut = state
                    .config
                    .actions
                    .get(name)
                    .ok_or_else(|| refused("unknown shortcut"))?;
                shortcut
                    .to_command()
                    .map(Action::Board)
                    .map_err(|e| refused(e.to_string()))
            } else {
                admit_action(action, &args)
            }
        })();
        drop(state);
        self.submit_at_epoch(action, session_id, controller, Some(epoch))
    }
    fn submit_at_epoch(
        &self,
        action: Result<Action>,
        session_id: Option<&str>,
        controller: Option<&str>,
        epoch: Option<u64>,
    ) -> CommandReply {
        let (reply, receiver) = oneshot::channel();
        let mut state = self.shared.state.lock().unwrap();
        let action = match action {
            Ok(action) => action,
            Err(error) => {
                let _ = reply.send(Err(error));
                return receiver;
            }
        };
        let status = state.value["status"].as_str().unwrap();
        let loading = matches!(action, Action::Load { .. });
        let refusal = if self.shared.closing.load(Ordering::Relaxed) {
            Some(refused("workbench is closing"))
        } else if epoch.is_some_and(|epoch| epoch != state.epoch) {
            Some(refused(
                "reload cancelled by a later stop or controller disconnect",
            ))
        } else if session_id != state.value["session_id"].as_str() {
            Some(refused("stale session; refresh state before submitting"))
        } else if self.shared.stop.load(Ordering::Relaxed) {
            Some(refused("board session is stopping"))
        } else if loading && state.cleanup_failed {
            Some(refused(
                "previous native cleanup failed; close this workbench before starting another session",
            ))
        } else if loading && (active(status) || state.pending_load) {
            Some(refused("a board session is already active"))
        } else if !loading && !matches!(status, "starting" | "paused" | "running") {
            Some(refused("load a board session first"))
        } else {
            None
        };
        if let Some(error) = refusal {
            let _ = reply.send(Err(error));
            return receiver;
        }
        if let Action::Load { config, .. } = &action {
            *self.close_timeout.lock().unwrap() =
                Duration::from_secs_f64(config.startup_timeout_secs)
                    .saturating_add(Duration::from_secs(35));
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
    pub async fn restart(
        &self,
        args: Value,
        session_id: Option<&str>,
        controller: Option<&str>,
    ) -> Result<Value> {
        // Admit the immutable launch before stopping a working session. The
        // epoch also covers the asynchronous gap between cleanup and enqueue.
        let (config, admission_epoch) = {
            let state = self.shared.state.lock().unwrap();
            (
                self.admit_launch(&args, state.firmware.as_ref())?,
                state.epoch,
            )
        };
        // Capture the new input before stopping a healthy instance. A linker
        // may still have removed or only partly written its output.
        let image = read_snapshot(config.firmware.as_ref().unwrap())
            .map_err(|error| refused(error.to_string()))?;
        let start = !args.as_object().is_some_and(|map| map.is_empty());
        let (stopped, epoch) = self.stop_at_epoch(session_id, Some(admission_epoch));
        stopped
            .await
            .map_err(|_| refused("workbench is closing"))??;
        self.submit_at_epoch(
            Ok(Action::Load {
                config,
                start,
                image: Some(image),
            }),
            session_id,
            controller,
            Some(epoch),
        )
        .await
        .map_err(|_| refused("workbench is closing"))?
    }
    pub fn request_stop(&self, session_id: Option<&str>) -> StopReply {
        self.stop_at_epoch(session_id, None).0
    }
    fn stop_at_epoch(
        &self,
        session_id: Option<&str>,
        admission_epoch: Option<u64>,
    ) -> (StopReply, u64) {
        let mut state = self.shared.state.lock().unwrap();
        if admission_epoch.is_some_and(|epoch| epoch != state.epoch) {
            let (tx, rx) = oneshot::channel();
            let _ = tx.send(Err(refused(
                "reload cancelled by a later stop or controller disconnect",
            )));
            return (rx.shared(), state.epoch);
        }
        if session_id.is_some() && session_id != state.value["session_id"].as_str() {
            let (tx, rx) = oneshot::channel();
            let _ = tx.send(Err(refused("stale session")));
            return (rx.shared(), state.epoch);
        }
        state.epoch += 1;
        self.shared.stop.store(true, Ordering::Relaxed);
        if active(state.value["status"].as_str().unwrap()) || state.pending_load {
            state.value["status"] = "stopping".into();
        }
        if state.stop.is_none() {
            let (sender, receiver) = oneshot::channel();
            state.stop = Some(PendingStop {
                sender,
                reply: receiver.shared(),
            });
        }
        (state.stop.as_ref().unwrap().reply.clone(), state.epoch)
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
                .recv_timeout(*self.close_timeout.lock().unwrap())
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
fn finish_stop(shared: &SharedState, state: &mut State, result: Result<Value>) {
    shared.stop.store(false, Ordering::Relaxed);
    if let Some(pending) = state.stop.take() {
        let _ = pending.sender.send(result);
    }
}
fn finish_idle_stop(shared: &SharedState, commands: &Receiver<Command>) {
    let mut state = shared.state.lock().unwrap();
    cancel_pending(commands);
    state.pending_load = false;
    if state.value["status"] == "stopping" {
        state.value["status"] = "stopped".into();
    }
    let completion = if state.cleanup_failed {
        Err(state.failure.clone().unwrap())
    } else {
        Ok(json!({"accepted":true}))
    };
    finish_stop(shared, &mut state, completion);
}
fn owner_main(shared: &SharedState, commands: &Receiver<Command>, options: &SessionOptions) {
    while !shared.closing.load(Ordering::Relaxed) {
        if shared.stop.load(Ordering::Relaxed) {
            finish_idle_stop(shared, commands);
        }
        let command = match commands.recv_timeout(Duration::from_millis(20)) {
            Ok(command) => command,
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        };
        if !matches!(command.action, Action::Load { .. }) {
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
    finish_idle_stop(shared, commands);
}
fn emit(shared: &SharedState, events: Vec<ChannelEvent>) {
    let mut state = shared.state.lock().unwrap();
    let session = state.value["session_id"].clone();
    for event in events {
        state
            .history
            .push(super::presentation::event(event), &session);
    }
}

fn observe(
    shared: &SharedState,
    lab: &mut ConnectedLab<'_>,
    snapshot: Value,
) -> sim_core::Result<()> {
    let running = lab.command(commands::query_status {})?.running;
    let remaining = lab.remaining()?.as_secs_f64();
    let (board, pins) = super::presentation::project(snapshot)?;
    let mut state = shared.state.lock().unwrap();
    state.value["status"] = if shared.stop.load(Ordering::Relaxed) {
        "stopping"
    } else if running {
        "running"
    } else {
        "paused"
    }
    .into();
    state.value["board"] = board;
    state.value["pins"] = pins;
    state.value["remaining_seconds"] = json!(remaining);
    state.value["usb"] = lab.usb_status();
    state.value["observation_stale"] = false.into();
    Ok(())
}
fn run_session(
    shared: &SharedState,
    commands: &Receiver<Command>,
    options: &SessionOptions,
    command: Command,
) {
    let Action::Load {
        config,
        mut start,
        mut image,
    } = command.action
    else {
        unreachable!()
    };
    let mut initial_reply = Some(command.reply);
    let mut epoch = command.epoch;
    let mut watch = None;
    loop {
        let launch = match config.lab_options(
            options.qemu.clone(),
            options.qmp_path.clone(),
            Some(shared.stop.clone()),
        ) {
            Ok(launch) => launch,
            Err(error) => {
                shared.state.lock().unwrap().pending_load = false;
                if let Some(reply) = initial_reply.take() {
                    let _ = reply.send(Err(refused(error.to_string())));
                }
                return;
            }
        };
        {
            let mut state = shared.state.lock().unwrap();
            state.pending_load = false;
            if epoch != state.epoch
                || shared.stop.load(Ordering::Relaxed)
                || shared.closing.load(Ordering::Relaxed)
            {
                if let Some(reply) = initial_reply.take() {
                    let _ = reply.send(Err(refused("load cancelled")));
                } else {
                    // The previous native instance is already gone. A stop or
                    // disconnect can still supersede this automatic continuation.
                    state.value["status"] = "stopping".into();
                    drop(state);
                    finish_idle_stop(shared, commands);
                }
                return;
            }
            state.value["status"] = "starting".into();
            state.value["session_id"] = Uuid::new_v4().simple().to_string().into();
            state.value["error"] = Value::Null;
            state.value["reload_error"] = Value::Null;
            state.value["notes"] = json!([]);
            state.failure = None;
            state.value["board"] = Value::Null;
            state.value["pins"] = json!([]);
            state.value["observation_stale"] = true.into();
            if state.firmware != config.firmware {
                state.value["firmware"] = json!({"name": config.firmware.as_ref().unwrap().file_name().unwrap().to_string_lossy()});
            }
            state.firmware = config.firmware.clone();
            state.config = config.clone();
            state.value["gdb_port"] = json!(config.gdb_port);
            state.value["config"] = json!(config);
        }
        let mut reload = None;
        let result = (|| -> std::result::Result<(), LabError> {
            if config.watch_elf && watch.is_none() {
                if let Some(snapshot) = &image {
                    watch = Some(FirmwareWatch::from_snapshot(
                        config.firmware.as_ref().unwrap(),
                        snapshot,
                    )?);
                } else {
                    let (watcher, snapshot) =
                        FirmwareWatch::new(config.firmware.as_ref().unwrap())?;
                    watch = Some(watcher);
                    image = Some(snapshot);
                }
            }
            let snapshot = match image.take() {
                Some(image) => image,
                None => read_snapshot(&launch.firmware)?,
            };
            super::runtime::with_firmware_image(launch, snapshot, |lab| {
                if start {
                    lab.start(config.startup, Duration::from_millis(config.user_hold_ms))?;
                }
                let (snapshot, events) = lab.pump()?;
                emit(shared, events);
                observe(shared, lab, snapshot)?;
                let cancelled =
                    shared.stop.load(Ordering::Relaxed) || shared.closing.load(Ordering::Relaxed);
                if let Some(reply) = initial_reply.take() {
                    let _ = reply.send(if cancelled {
                        Err(refused("load cancelled"))
                    } else {
                        Ok(json!({"accepted":true}))
                    });
                }
                let mut next_observation = Instant::now() + Duration::from_millis(100);
                let mut next_file_poll = Instant::now() + Duration::from_millis(250);
                while !shared.stop.load(Ordering::Relaxed)
                    && !shared.closing.load(Ordering::Relaxed)
                {
                    if shared.release.swap(false, Ordering::Relaxed) {
                        lab.inject(LabInput::Key(false))?;
                        shared.state.lock().unwrap().key_controller = None;
                    }
                    let current = next_command(shared, commands);
                    let had_command = current.is_some();
                    if let Some(current) = current {
                        let action = match current.action {
                            Action::Load { .. } => Err(Arc::new(Error::Admission(
                                "a board session is already active".into(),
                            )) as Failure),
                            Action::Board(command) => {
                                let pressed = match &command {
                                    BoardCommand::Input(LabInput::Key(value)) => Some(*value),
                                    _ => None,
                                };
                                lab.apply(command).inspect(|_| {
                                    if let Some(pressed) = pressed {
                                        key_applied(
                                            shared,
                                            current.controller,
                                            current.epoch,
                                            pressed,
                                        );
                                    }
                                })
                            }
                        };
                        match action {
                            Ok(timestamp_ns) => {
                                let _ = current.reply.send(Ok(json!({"accepted":true,"timestamp_ns":timestamp_ns.map(|time| time.to_string())})));
                            }
                            Err(error) => {
                                let retryable = error.downcast_ref::<AdcPausedError>().is_some()
                                    || matches!(
                                        error.downcast_ref::<Error>(),
                                        Some(Error::Admission(_))
                                    )
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
                    if Instant::now() >= next_file_poll {
                        next_file_poll = Instant::now() + Duration::from_millis(250);
                        if let Some(watcher) = &mut watch {
                            match watcher.poll(Instant::now()) {
                                Ok(candidate) => {
                                    shared.state.lock().unwrap().value["reload_error"] =
                                        Value::Null;
                                    if let Some(snapshot) = candidate {
                                        let running =
                                            lab.command(commands::query_status {})?.running;
                                        let mut state = shared.state.lock().unwrap();
                                        if !shared.stop.load(Ordering::Relaxed)
                                            && !shared.closing.load(Ordering::Relaxed)
                                        {
                                            reload = Some((snapshot, state.epoch, running));
                                            state.value["status"] = "stopping".into();
                                            break;
                                        }
                                    }
                                }
                                Err(error) => {
                                    shared.state.lock().unwrap().value["reload_error"] =
                                        error.to_string().into();
                                }
                            }
                        }
                    }
                    lab.wait(None, Duration::from_millis(5))?;
                }
                shared.state.lock().unwrap().value["status"] = "stopping".into();
                Ok(())
            })
        })();
        let continue_reload = finish_session(
            shared,
            commands,
            result,
            config.usbip.is_some(),
            initial_reply.take(),
            reload.as_ref().map(|(_, epoch, _)| *epoch),
        );
        if !continue_reload {
            return;
        }
        let (snapshot, next_epoch, running) = reload.unwrap();
        image = Some(snapshot);
        epoch = next_epoch;
        start = running;
    }
}

fn finish_session(
    shared: &SharedState,
    commands: &Receiver<Command>,
    result: std::result::Result<(), LabError>,
    usb_enabled: bool,
    initial_reply: Option<oneshot::Sender<Result<Value>>>,
    reload_epoch: Option<u64>,
) -> bool {
    // Queue disposal and terminal publication share the admission lock. Once a
    // client sees stopped/failed, its next launch belongs to the next session.
    let mut state = shared.state.lock().unwrap();
    cancel_pending(commands);
    state.pending_load = false;
    state.key_controller = None;
    shared.release.store(false, Ordering::Relaxed);
    let result = match result {
        Err(error)
            if !error.cleanup_failed
                && shared.stop.load(Ordering::Relaxed)
                && intentional_stop(&error.primary) =>
        {
            Ok(error.notes)
        }
        other => other.map(|()| vec![]),
    };
    let continue_reload = result.is_ok()
        && reload_epoch == Some(state.epoch)
        && !shared.stop.load(Ordering::Relaxed)
        && !shared.closing.load(Ordering::Relaxed);
    let completion = match result {
        Ok(notes) => {
            if let Some(sender) = initial_reply {
                let _ = sender.send(Err(refused("load cancelled")));
            }
            state.value["status"] = if continue_reload {
                "starting"
            } else {
                "stopped"
            }
            .into();
            state.value["observation_stale"] = true.into();
            state.value["remaining_seconds"] = Value::Null;
            state.value["notes"] = json!(notes);
            state.value["usb"] =
                json!({"state": if usb_enabled { "disconnected" } else { "disabled" }});
            Ok(json!({"accepted":true}))
        }
        Err(LabError {
            primary,
            notes,
            cleanup_failed,
        }) => {
            let error = SessionError {
                primary,
                notes,
                input: false,
            };
            if let Some(sender) = initial_reply {
                let _ = sender.send(Err(error.clone()));
            }
            state.value["status"] = "failed".into();
            state.value["error"] = error.to_string().into();
            state.value["notes"] = json!(error.notes);
            state.value["observation_stale"] = true.into();
            state.value["remaining_seconds"] = Value::Null;
            state.cleanup_failed = cleanup_failed;
            state.failure = Some(error.clone());
            Err(error)
        }
    };
    finish_stop(shared, &mut state, completion);
    continue_reload
}
fn intentional_stop(error: &Failure) -> bool {
    matches!(error.downcast_ref::<Error>(), Some(Error::Interrupted(_)))
        || error
            .downcast_ref::<crate::native_process::Error>()
            .is_some_and(|error| error.kind == crate::native_process::ErrorKind::Interrupted)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn paused_session(capacity: usize) -> (LabSession, Receiver<Command>) {
        let shared = Arc::new(SharedState {
            state: Mutex::new(State {
                value: json!({"status":"paused","session_id":"current"}),
                firmware: None,
                config: LabProject::default(),
                epoch: 0,
                pending_load: false,
                key_controller: None,
                stop: None,
                history: History::new(),
                failure: None,
                cleanup_failed: false,
            }),
            stop: Arc::new(AtomicBool::new(false)),
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
                close_timeout: Mutex::new(Duration::ZERO),
                project_file: None,
                project_config: Mutex::new(LabProject::default()),
            },
            commands,
        )
    }
    #[test]
    fn pure_admission_keeps_bytes_and_rejects_bool_fields() {
        let Action::Board(BoardCommand::Input(LabInput::Uart { data, .. })) =
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
        let (board, pins) = super::super::presentation::project(
            json!({"schema":1,"timestamp_ns":9007199254741009u64,
            "board":"dm-mc02","vin_mv":24000,"gpio":gpio,
            "key":{"pin":"A15","pressed":false,"level":0},
            "led":{"pin":"A7","rgb":0,"brightness":0,"source":"gpio-latch"}}),
        )
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
        finish_idle_stop(&session.shared, &commands);
        assert_eq!(
            futures::executor::block_on(stopped).unwrap().unwrap(),
            json!({"accepted":true})
        );
    }
    fn select_test_firmware(session: &LabSession) -> tempfile::NamedTempFile {
        let firmware = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(firmware.path(), super::super::firmware_image::tests::elf(1)).unwrap();
        session.shared.state.lock().unwrap().firmware = Some(firmware.path().to_owned());
        firmware
    }
    #[test]
    fn later_stop_cancels_reload_waiting_for_cleanup_and_next_launch_survives() {
        let (session, commands) = paused_session(2);
        let _firmware = select_test_firmware(&session);
        let mut reload = Box::pin(session.restart(json!({}), Some("current"), Some("page")));
        assert!(reload.as_mut().now_or_never().is_none());
        let stop = session.request_stop(Some("current"));
        finish_session(&session.shared, &commands, Ok(()), false, None, None);
        futures::executor::block_on(stop).unwrap().unwrap();
        let error = futures::executor::block_on(reload).unwrap_err();
        assert!(error.input && error.to_string().contains("later stop"));
        assert!(
            commands.try_recv().is_err(),
            "cancelled reload must not launch"
        );

        let next = session.submit("load", json!({}), Some("current"), Some("page"));
        let command = next_command(&session.shared, &commands).expect("new launch remains queued");
        command.reply.send(Ok(json!({"accepted":true}))).unwrap();
        futures::executor::block_on(next).unwrap().unwrap();
    }
    #[test]
    fn incomplete_rebuild_is_rejected_before_manual_reload_stops_the_board() {
        let (session, commands) = paused_session(1);
        let firmware = select_test_firmware(&session);
        std::fs::write(firmware.path(), b"\x7fELF").unwrap();
        let error =
            futures::executor::block_on(session.restart(json!({}), Some("current"), Some("page")))
                .unwrap_err();
        assert!(error.input && error.to_string().contains("incomplete or invalid ELF"));
        assert_eq!(session.state()["status"], "paused");
        assert!(commands.try_recv().is_err());
    }
    #[test]
    fn disconnect_during_image_capture_cancels_reload_before_it_stops_the_board() {
        let (session, commands) = paused_session(1);
        let admission_epoch = session.shared.state.lock().unwrap().epoch;
        session.disconnect("page");
        let (stopped, _) = session.stop_at_epoch(Some("current"), Some(admission_epoch));
        let error = futures::executor::block_on(stopped).unwrap().unwrap_err();
        assert!(error.input && error.to_string().contains("controller disconnect"));
        assert_eq!(session.state()["status"], "paused");
        let input = session.submit("vin", json!({"millivolts":12000}), Some("current"), None);
        let command = next_command(&session.shared, &commands).expect("board still accepts input");
        command.reply.send(Ok(json!({"accepted":true}))).unwrap();
        futures::executor::block_on(input).unwrap().unwrap();
    }
    #[test]
    fn automatic_reload_keeps_launch_admission_closed_but_yields_to_later_stop() {
        for stop in [false, true] {
            let (session, commands) = paused_session(1);
            let reply = stop.then(|| session.request_stop(None));
            let resume = finish_session(&session.shared, &commands, Ok(()), false, None, Some(0));
            assert_eq!(resume, !stop);
            if let Some(reply) = reply {
                futures::executor::block_on(reply).unwrap().unwrap();
                assert_eq!(session.state()["status"], "stopped");
            } else {
                assert_eq!(session.state()["status"], "starting");
                let _firmware = select_test_firmware(&session);
                let rejected = futures::executor::block_on(session.submit(
                    "load",
                    json!({}),
                    Some("current"),
                    None,
                ))
                .unwrap()
                .unwrap_err();
                assert!(rejected.input && rejected.to_string().contains("already active"));
            }
        }
    }
    #[test]
    fn terminal_state_cancels_old_queue_before_admitting_replacement() {
        let (session, commands) = paused_session(2);
        let _firmware = select_test_firmware(&session);
        session.shared.state.lock().unwrap().value["status"] = "stopped".into();
        let old = session.submit("load", json!({}), Some("current"), None);
        let stop = session.request_stop(Some("current"));
        let rejected = session.submit("load", json!({}), Some("current"), None);
        assert!(futures::executor::block_on(rejected).unwrap().is_err());
        finish_idle_stop(&session.shared, &commands);
        assert!(futures::executor::block_on(old).unwrap().is_err());
        futures::executor::block_on(stop).unwrap().unwrap();
        assert_eq!(session.state()["status"], "stopped");
        let next = session.submit("load", json!({}), Some("current"), None);
        let command = commands
            .try_recv()
            .expect("replacement is admitted after final state");
        command.reply.send(Ok(json!({"accepted":true}))).unwrap();
        futures::executor::block_on(next).unwrap().unwrap();
    }
    #[test]
    fn stop_only_normalizes_its_own_interruption_after_successful_cleanup() {
        for (primary, cleanup_failed, notes, stopped) in [
            (
                Error::Interrupted("Lab interrupted".into()),
                false,
                vec![],
                true,
            ),
            (
                Error::Interrupted("Lab interrupted".into()),
                false,
                vec!["native stderr tail:\nstartup diagnostic".into()],
                true,
            ),
            (
                Error::Interrupted("Lab interrupted".into()),
                true,
                vec!["cleanup failed".into()],
                false,
            ),
            (
                Error::State("QEMU disconnected".into()),
                false,
                vec![],
                false,
            ),
        ] {
            let (session, commands) = paused_session(1);
            let reply = session.request_stop(Some("current"));
            let expected_notes = notes.clone();
            finish_session(
                &session.shared,
                &commands,
                Err(LabError {
                    primary: Arc::new(primary),
                    notes,
                    cleanup_failed,
                }),
                false,
                None,
                None,
            );
            assert_eq!(futures::executor::block_on(reply).unwrap().is_ok(), stopped);
            assert_eq!(
                session.state()["status"],
                if stopped { "stopped" } else { "failed" }
            );
            assert_eq!(session.state()["notes"], json!(expected_notes));
        }
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
