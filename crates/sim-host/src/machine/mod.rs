//! Native execution owner. Profiles borrow this connection and exact-target
//! boundary; they never launch a process or advance a second clock.

pub use crate::cosim_session;

use crate::{
    launch,
    native_process::{NativeProcess, ProcessOptions},
    qmp::{commands, PreparedCommand, QmpSession, QueryVirtualExecution, RunToVirtualTime},
    runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry},
};
use serde_json::Value;
use sim_core::{
    BuildContext, Component, Error, Factory, Failure, HealthCheck, Input, InputCommit,
    InputValidator, Machine, NodeDescription, NodeSpec, Result, Session, StepContext, StepResult,
    Topology,
};
use std::{
    cell::RefCell,
    ffi::OsString,
    path::PathBuf,
    rc::Rc,
    sync::Arc,
    time::{Duration, Instant},
};

pub(crate) fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
pub(crate) fn state_error(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}
pub(crate) fn backend_error(message: impl Into<String>) -> Failure {
    Arc::new(Error::BackendUnavailable(message.into()))
}

#[derive(Clone, Debug)]
pub struct QemuOptions {
    pub firmware: PathBuf,
    pub qemu: Option<PathBuf>,
    pub backend_root: Option<PathBuf>,
    pub uarts: Vec<String>,
    pub remote: bool,
    pub adc: bool,
    pub gdb_port: Option<u16>,
    pub qmp_path: Option<PathBuf>,
}
impl QemuOptions {
    pub fn new(firmware: impl Into<PathBuf>) -> Self {
        Self {
            firmware: firmware.into(),
            qemu: None,
            backend_root: None,
            uarts: vec!["uart1".into()],
            remote: false,
            adc: false,
            gdb_port: None,
            qmp_path: None,
        }
    }
}

/// Board-specific wire projection. Socket IO and virtual targets remain with
/// the backend. Each method can retain partial effects on failure.
pub trait ProfileAdapter {
    fn streams(&self) -> Vec<String>;
    fn machine_arguments(&self) -> Vec<OsString>;
    fn serial_slots(&self) -> &'static [&'static str];
    fn start(&mut self, backend: &mut NativeBackend) -> Result<()>;
    fn complete_cut(&mut self, backend: &mut NativeBackend, cut: u64, target: u64) -> Result<()>;
    fn input(&mut self, backend: &mut NativeBackend, port: &str, value: HostInput) -> Result<()>;
    fn commit_inputs(&mut self, backend: &mut NativeBackend, cut: u64) -> Result<()>;
    fn execution(&mut self, backend: &mut NativeBackend) -> Result<(Value, Value)> {
        backend.query_execution()
    }
    fn service(&mut self, _backend: &mut NativeBackend, _cut: u64) -> Result<()> {
        Ok(())
    }
    fn observe(&self) -> Result<Value>;
    fn close(&mut self) -> Result<()>;
}

/// Borrowable native access; the enclosing machine is the only lifecycle owner.
/// The synchronous core drives the existing async QMP session on this runtime.
pub struct NativeBackend {
    context: HostContext,
    options: QemuOptions,
    process: Rc<RefCell<Option<NativeProcess>>>,
    runtime: Option<tokio::runtime::Runtime>,
    qmp: Option<QmpSession>,
    origin: Option<u64>,
    session: Option<Session>,
    last_time: u64,
    prepared: Option<StepContext>,
    pending: Option<StepContext>,
    closed: bool,
}
impl NativeBackend {
    pub fn context(&self) -> &HostContext {
        &self.context
    }
    pub fn native_origin_ns(&self) -> Result<u64> {
        self.origin
            .ok_or_else(|| state_error("QEMU has no native origin"))
    }
    pub fn t_sim_ns(&self) -> u64 {
        self.last_time
    }
    pub fn pending(&self) -> Option<StepContext> {
        self.pending
    }
    pub fn deadline(&self) -> Result<Instant> {
        self.context
            .deadline()?
            .ok_or_else(|| state_error("QEMU requires a finite host deadline"))
    }
    pub fn check(&self) -> Result<()> {
        self.context.check_io()?;
        if self.closed {
            return Err(state_error("QEMU machine is closed"));
        }
        self.context.check_deadline()
    }
    pub fn to_native(&self, time: u64) -> Result<u64> {
        let session = self
            .session
            .ok_or_else(|| state_error("QEMU session has not started"))?;
        time.checked_sub(session.t_sim_ns)
            .and_then(|delta| self.origin?.checked_add(delta))
            .filter(|time| *time <= i64::MAX as u64)
            .ok_or_else(|| admission("mapped QEMU target exceeds signed64 nonnegative nanoseconds"))
    }
    pub fn to_sim(&self, time: u64) -> Result<u64> {
        if time > i64::MAX as u64 {
            return Err(admission("QEMU source timestamp exceeds signed64"));
        }
        time.checked_sub(self.native_origin_ns()?)
            .and_then(|delta| self.session?.t_sim_ns.checked_add(delta))
            .ok_or_else(|| {
                admission("QEMU source timestamp precedes origin or mapped time exceeds u64")
            })
    }
    pub fn command<C: commands::QmpCommand>(&mut self, command: C) -> Result<C::Ok> {
        self.check()?;
        let deadline = self.deadline()?;
        let result = self
            .runtime
            .as_ref()
            .ok_or_else(|| state_error("QEMU runtime is not started"))?
            .block_on(
                self.qmp
                    .as_mut()
                    .ok_or_else(|| state_error("QMP is not connected"))?
                    .command(command, deadline),
            );
        let result = result.map_err(|error| self.context.fail(error))?;
        self.check()?;
        Ok(result)
    }
    pub fn command_many(&mut self, commands: Vec<PreparedCommand>) -> Result<Vec<Value>> {
        self.check()?;
        let deadline = self.deadline()?;
        let result = self
            .runtime
            .as_ref()
            .ok_or_else(|| state_error("QEMU runtime is not started"))?
            .block_on(
                self.qmp
                    .as_mut()
                    .ok_or_else(|| state_error("QMP is not connected"))?
                    .command_many(commands, deadline),
            );
        let values = result.map_err(|error| self.context.fail(error))?;
        self.check()?;
        Ok(values)
    }
    pub fn query_execution(&mut self) -> Result<(Value, Value)> {
        let mut values = self.command_many(vec![
            prepared(QueryVirtualExecution {})?,
            prepared(commands::query_status {})?,
        ])?;
        let status = values.pop().expect("QMP batch arity");
        Ok((values.pop().expect("QMP batch arity"), status))
    }
    pub fn property(&mut self, name: &str) -> Result<Value> {
        self.command(commands::qom_get {
            path: "/machine".into(),
            property: name.into(),
        })
    }
    pub fn set_property(&mut self, name: &str, value: Value) -> Result<()> {
        self.command(commands::qom_set {
            path: "/machine".into(),
            property: name.into(),
            value,
        })?;
        Ok(())
    }
    pub fn connect_stream(&self, name: &str) -> Result<std::os::unix::net::UnixStream> {
        self.check()?;
        let path = self.socket(name)?;
        let deadline = self
            .deadline()?
            .min(Instant::now() + Duration::from_secs(2));
        let stream = self
            .runtime
            .as_ref()
            .ok_or_else(|| state_error("QEMU runtime is not started"))?
            .block_on(async {
                tokio::time::timeout_at(deadline.into(), tokio::net::UnixStream::connect(path))
                    .await
            })
            .map_err(|_| {
                self.context.fail(Arc::new(Error::Deadline(
                    "QEMU stream connect deadline expired".into(),
                )))
            })?
            .map_err(|error| self.context.fail(Arc::new(error)))?;
        let stream = stream
            .into_std()
            .map_err(|error| self.context.fail(Arc::new(error)))?;
        self.check()?;
        Ok(stream)
    }
    pub fn socket(&self, name: &str) -> Result<PathBuf> {
        self.process
            .borrow()
            .as_ref()
            .and_then(|owner| owner.socket(name))
            .map(PathBuf::from)
            .ok_or_else(|| state_error("QEMU socket is not owned"))
    }
    pub fn connection_info(&self) -> Result<ConnectionInfo> {
        let process = self.process.borrow();
        let owner = process
            .as_ref()
            .ok_or_else(|| state_error("QEMU has not started"))?;
        Ok(ConnectionInfo {
            pid: owner.id().ok_or_else(|| state_error("QEMU was reaped"))?,
            directory: owner.directory().map(PathBuf::from),
            native_origin_ns: self.native_origin_ns()?,
        })
    }
    fn start(&mut self, adapter: &dyn ProfileAdapter) -> Result<()> {
        self.check()?;
        let selected = self
            .options
            .qemu
            .clone()
            .or_else(|| {
                std::env::var_os("QEMU_SYSTEM_ARM")
                    .filter(|value| !value.is_empty())
                    .map(PathBuf::from)
            })
            .or_else(|| {
                self.options
                    .backend_root
                    .as_ref()
                    .map(|root| root.join("build/qemu/qemu-system-arm"))
            })
            .or_else(|| {
                let default = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("../../build/qemu/qemu-system-arm");
                default.is_file().then_some(default)
            });
        let qemu = launch::resolve_qemu(selected).map_err(|error| backend_error(error.message))?;
        self.check()?;
        let streams = adapter.streams();
        let mut argv = launch::native_qemu_command(
            &qemu,
            adapter.machine_arguments(),
            adapter.serial_slots(),
            &streams,
            self.options.qmp_path.as_deref(),
            self.options.gdb_port,
        )
        .map_err(|error| admission(error.message))?;
        argv.extend(
            [
                "-accel",
                "tcg,thread=single",
                "-icount",
                "shift=0,align=off,sleep=off",
            ]
            .map(OsString::from),
        );
        let deadline = self.deadline()?;
        let sockets = std::iter::once("qmp".into()).chain(streams).collect();
        let owner = NativeProcess::start(ProcessOptions {
            argv,
            sockets,
            wait_sockets: None,
            startup_timeout: deadline.saturating_duration_since(Instant::now()),
            host_deadline: deadline,
        })
        .map_err(|error| self.context.fail(error))?;
        *self.process.borrow_mut() = Some(owner);
        self.check()?;
        self.runtime = Some(
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|error| self.context.fail(Arc::new(error)))?,
        );
        let path = self.socket("qmp")?;
        self.qmp = Some(
            self.runtime
                .as_ref()
                .unwrap()
                .block_on(QmpSession::connect_uds(path, deadline))
                .map_err(|error| self.context.fail(error))?,
        );
        let commands = self.command(commands::query_commands {})?;
        if !["x-run-to-virtual-time", "x-query-virtual-execution"]
            .iter()
            .all(|required| commands.iter().any(|command| &command.name == required))
        {
            return Err(backend_error(
                "QEMU lacks required native virtual execution commands",
            ));
        }
        let (execution, status) = self.query_execution()?;
        let origin = native_time(&execution["current-ns"])?;
        validate_execution(&execution, &status, origin, true)?;
        self.origin = Some(origin);
        Ok(())
    }
    pub fn wait_poll(&self) -> Result<()> {
        self.check()?;
        let remaining = self.deadline()?.saturating_duration_since(Instant::now());
        std::thread::sleep(Duration::from_millis(1).min(remaining));
        self.check()
    }
    fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        let mut errors: Vec<Failure> = vec![];
        if let Some(owner) = self.process.borrow_mut().as_mut() {
            if let Err(error) = owner.begin_cleanup() {
                errors.push(error);
            }
        }
        if let (Some(runtime), Some(qmp)) = (&self.runtime, &mut self.qmp) {
            if let Err(error) = runtime.block_on(qmp.close()) {
                errors.push(error);
            }
        }
        self.qmp.take();
        self.runtime.take();
        if let Some(mut owner) = self.process.borrow_mut().take() {
            if let Err(error) = owner.close() {
                errors.push(error);
            }
        }
        cleanup_result(errors)
    }
}

#[derive(Clone, Debug)]
pub struct ConnectionInfo {
    pub pid: u32,
    pub directory: Option<PathBuf>,
    pub native_origin_ns: u64,
}

pub(crate) fn prepared<C: commands::QmpCommand>(command: C) -> Result<PreparedCommand> {
    PreparedCommand::new(command).map_err(|error| admission(error.to_string()))
}
pub(crate) fn native_time(value: &Value) -> Result<u64> {
    value
        .as_u64()
        .filter(|time| *time <= i64::MAX as u64)
        .ok_or_else(|| state_error("native time must be signed64 nonnegative nanoseconds"))
}
pub(crate) fn validate_status(status: &Value, initial: bool) -> Result<()> {
    if status["running"] != false
        || !(status["status"] == "paused" || initial && status["status"] == "prelaunch")
    {
        return Err(state_error(
            "QEMU must remain paused at the completed target",
        ));
    }
    Ok(())
}
pub(crate) fn validate_execution(
    execution: &Value,
    status: &Value,
    target: u64,
    initial: bool,
) -> Result<()> {
    if native_time(&execution["current-ns"])? != target
        || execution["state"] != if initial { "idle" } else { "reached" }
        || if initial {
            execution.get("target-ns").is_some()
        } else {
            native_time(&execution["target-ns"])? != target
        }
    {
        return Err(state_error(
            "QEMU completed cut changed outside Simulation control",
        ));
    }
    validate_status(status, initial)
}

#[derive(Debug)]
struct CleanupErrors {
    primary: Failure,
    later: Vec<String>,
}
impl std::fmt::Display for CleanupErrors {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.primary)?;
        for note in &self.later {
            write!(f, "; cleanup: {note}")?;
        }
        Ok(())
    }
}
impl std::error::Error for CleanupErrors {}
pub(crate) fn cleanup_result(mut errors: Vec<Failure>) -> Result<()> {
    if errors.is_empty() {
        return Ok(());
    }
    let primary = errors.remove(0);
    if errors.is_empty() {
        return Err(primary);
    }
    Err(Arc::new(CleanupErrors {
        primary,
        later: errors.into_iter().map(|error| error.to_string()).collect(),
    }))
}

pub struct QemuMachine {
    backend: Rc<RefCell<NativeBackend>>,
    adapter: Box<dyn ProfileAdapter>,
}
impl QemuMachine {
    pub fn connection_info(&self) -> Result<ConnectionInfo> {
        self.backend.borrow().connection_info()
    }
    pub fn observe(&self) -> Result<Value> {
        self.adapter.observe()
    }
    pub fn native_origin_ns(&self) -> Result<u64> {
        self.backend.borrow().native_origin_ns()
    }
    pub fn command<C: commands::QmpCommand>(&mut self, command: C) -> Result<C::Ok> {
        self.backend.borrow_mut().command(command)
    }
    fn validate_cut(&mut self) -> Result<()> {
        let mut backend = self.backend.borrow_mut();
        let (execution, status) = self.adapter.execution(&mut backend)?;
        validate_execution(
            &execution,
            &status,
            backend.to_native(backend.last_time)?,
            backend.last_time == backend.session.unwrap().t_sim_ns,
        )
        .map_err(|error| backend.context.fail(error))
    }
}
impl Component<HostInput, HostEvent> for QemuMachine {
    fn close(&mut self) -> Result<()> {
        let mut errors = vec![];
        if let Err(error) = self.adapter.close() {
            errors.push(error);
        }
        if let Err(error) = self.backend.borrow_mut().close() {
            errors.push(error);
        }
        cleanup_result(errors)
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        let mut backend = self.backend.borrow_mut();
        backend.session = Some(session);
        backend.last_time = session.t_sim_ns;
        Ok(())
    }
    fn start(&mut self, _: &HostContext) -> Result<()> {
        let result = (|| {
            let mut backend = self.backend.borrow_mut();
            backend.start(self.adapter.as_ref())?;
            self.adapter.start(&mut backend)
        })();
        result.map_err(|error| self.backend.borrow().context.fail(error))
    }
    fn machine(&mut self) -> Option<&mut dyn Machine<HostInput, HostEvent>> {
        Some(self)
    }
    fn input(&mut self) -> Option<&mut dyn Input<HostInput, HostEvent>> {
        Some(self)
    }
    fn input_commit(&mut self) -> Option<&mut dyn InputCommit<HostInput, HostEvent>> {
        Some(self)
    }
    fn health_check(&self) -> Option<HealthCheck> {
        let process = Rc::downgrade(&self.backend.borrow().process);
        let context = self.backend.borrow().context.clone();
        Some(Rc::new(move || {
            if let Some(process) = process.upgrade() {
                if let Some(owner) = process.borrow_mut().as_mut() {
                    owner
                        .check_with_deadline(
                            context
                                .deadline()?
                                .ok_or_else(|| state_error("QEMU requires finite deadline"))?,
                        )
                        .map_err(|error| error as Failure)?;
                }
            }
            Ok(())
        }))
    }
}
impl Machine<HostInput, HostEvent> for QemuMachine {
    fn prepare_step(&mut self, step: &StepContext, _: &HostContext) -> Result<()> {
        self.backend.borrow().check()?;
        {
            let backend = self.backend.borrow();
            if backend.process.borrow().is_none()
                || backend.pending.is_some()
                || backend.prepared.is_some()
                || Some(step.session_id) != backend.session.map(|session| session.session_id)
                || step.t_sim_ns.checked_sub(step.dt_ns) != Some(backend.last_time)
            {
                return Err(state_error(
                    "QEMU step does not continue the current completed session",
                ));
            }
            backend.to_native(step.t_sim_ns)?;
        }
        self.validate_cut()?;
        self.backend.borrow_mut().prepared = Some(*step);
        Ok(())
    }
    fn request_step(&mut self, step: &StepContext, _: &HostContext) -> Result<()> {
        let mut backend = self.backend.borrow_mut();
        backend.check()?;
        if backend.prepared != Some(*step) || backend.pending.is_some() {
            return Err(state_error("QEMU request requires its prepared step"));
        }
        let target = backend.to_native(step.t_sim_ns)?;
        let last_time = backend.last_time;
        self.adapter.service(&mut backend, last_time)?;
        backend.command(RunToVirtualTime {
            target_ns: target as i64,
        })?;
        backend.pending = Some(*step);
        backend.prepared = None;
        Ok(())
    }
    fn finish_step(
        &mut self,
        step: &StepContext,
        _: &HostContext,
    ) -> Result<StepResult<HostEvent>> {
        let result = (|| {
            let mut backend = self.backend.borrow_mut();
            backend.check()?;
            if backend.pending != Some(*step) {
                return Err(state_error("QEMU completion requires its pending step"));
            }
            let target = backend.to_native(step.t_sim_ns)?;
            let mut previous = backend.to_native(backend.last_time)?;
            loop {
                self.adapter.service(&mut backend, step.t_sim_ns)?;
                let (execution, status) = self.adapter.execution(&mut backend)?;
                let current = native_time(&execution["current-ns"])?;
                if native_time(&execution["target-ns"])? != target
                    || !(previous..=target).contains(&current)
                {
                    return Err(state_error("native execution target or time changed"));
                }
                if execution["state"] == "reached" {
                    validate_execution(&execution, &status, target, false)?;
                    break;
                }
                if execution["state"] != "running" {
                    return Err(state_error("native execution target was interrupted"));
                }
                previous = current;
                backend.wait_poll()?;
            }
            self.adapter.service(&mut backend, step.t_sim_ns)?;
            self.adapter
                .complete_cut(&mut backend, step.t_sim_ns, target)?;
            backend.check()?;
            let (execution, status) = self.adapter.execution(&mut backend)?;
            validate_execution(&execution, &status, target, false)?;
            backend.check()?;
            backend.last_time = step.t_sim_ns;
            backend.pending = None;
            Ok(StepResult::reached(step.t_sim_ns))
        })();
        result.map_err(|error| self.backend.borrow().context.fail(error))
    }
}
impl Input<HostInput, HostEvent> for QemuMachine {
    fn input(&mut self, port: &str, value: HostInput, _: &HostContext) -> Result<()> {
        self.validate_cut()?;
        let result = {
            let mut backend = self.backend.borrow_mut();
            backend.check()?;
            if backend.pending.is_some() {
                return Err(backend
                    .context
                    .fail(state_error("QEMU input requires a completed paused cut")));
            }
            self.adapter.input(&mut backend, port, value)
        };
        result.map_err(|error| self.backend.borrow().context.fail(error))
    }
}
impl InputCommit<HostInput, HostEvent> for QemuMachine {
    fn commit_inputs(&mut self, cut: u64, _: &HostContext) -> Result<()> {
        let result = {
            let mut backend = self.backend.borrow_mut();
            backend.check()?;
            if backend.pending.is_some() || cut != backend.last_time {
                return Err(backend
                    .context
                    .fail(state_error("QEMU input commit requires its completed cut")));
            }
            self.adapter.commit_inputs(&mut backend, cut)
        };
        result.map_err(|error| self.backend.borrow().context.fail(error))
    }
}

pub struct QemuFactory;
impl InputValidator<HostOptions, HostInput> for QemuFactory {
    fn validate_input(
        &self,
        node: &NodeSpec<HostOptions>,
        port: &str,
        value: HostInput,
        connected: &[String],
    ) -> Result<HostInput> {
        crate::profile::validate_input(node, port, value, connected)
    }
}
impl Factory<HostOptions, HostInput, HostEvent> for QemuFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        crate::profile::describe(node)
    }
    fn input_validator(&self) -> Option<&dyn InputValidator<HostOptions, HostInput>> {
        Some(self)
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        descriptions: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        crate::profile::validate_topology(topology, descriptions)
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::Qemu(options) = &node.options else {
            return Err(admission("machine.qemu requires QEMU options"));
        };
        let backend = Rc::new(RefCell::new(NativeBackend {
            context: build.context(),
            options: options.clone(),
            process: Rc::new(RefCell::new(None)),
            runtime: None,
            qmp: None,
            origin: None,
            session: None,
            last_time: 0,
            prepared: None,
            pending: None,
            closed: false,
        }));
        let adapter = crate::profile::DmMc02Adapter::create(node, build, Rc::downgrade(&backend))?;
        Ok(Box::new(QemuMachine {
            backend,
            adapter: Box::new(adapter),
        }))
    }
}

impl Drop for QemuMachine {
    fn drop(&mut self) {
        let _ = <Self as Component<HostInput, HostEvent>>::close(self);
    }
}
