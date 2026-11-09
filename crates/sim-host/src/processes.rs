//! Explicitly trusted literal host commands. Simulation owns lifecycle; health
//! callbacks only observe exits, and sampled cuts own bounded pipe diagnostics.

use crate::{
    native_process::{exit_status, stop_child_group},
    runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry},
};
use rustix::process::{pidfd_open, Pid, PidfdFlags};
use sim_core::{
    BuildContext, Capability, Component, Error, EventTime, Factory, Failure, HealthCheck,
    NodeDescription, NodeSpec, Result, Sampler, Topology,
};
use std::{
    cell::RefCell,
    collections::BTreeSet,
    io::{self, Read},
    os::{
        fd::{AsFd, OwnedFd},
        unix::process::CommandExt,
    },
    path::{Component as PathComponent, Path, PathBuf},
    process::{Child, Command, Stdio},
    rc::Rc,
    sync::Arc,
};

fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn failed(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}

/// An admitted executable keeps its absolute launch spelling, including symlinks.
#[derive(Clone, Debug)]
pub struct HostCommand {
    name: String,
    argv: Vec<String>,
    cwd: PathBuf,
}
impl HostCommand {
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn argv(&self) -> &[String] {
        &self.argv
    }
    pub fn cwd(&self) -> &Path {
        &self.cwd
    }
    pub fn admit(name: String, mut argv: Vec<String>, cwd: PathBuf) -> Result<Self> {
        if !(1..=64).contains(&name.len())
            || !name.bytes().enumerate().all(|(i, b)| {
                b.is_ascii_alphabetic() || b == b'_' || i > 0 && (b.is_ascii_digit() || b == b'-')
            })
        {
            return Err(admission(
                "command name must be an ASCII identifier of 1..64 characters",
            ));
        }
        if !cwd.is_absolute()
            || cwd.as_os_str().as_encoded_bytes().contains(&0)
            || !(1..=128).contains(&argv.len())
            || argv
                .iter()
                .any(|arg| arg.len() > 4096 || arg.contains('\0'))
            || argv[0].trim().is_empty()
        {
            return Err(admission(
                "commands require absolute cwd and 1..128 arguments, each <=4096 bytes without NUL",
            ));
        }
        let executable = if argv[0].contains('/') {
            cwd.join(&argv[0])
        } else {
            let search = std::env::join_paths(
                std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
                    .map(|path| cwd.join(path)),
            )
            .map_err(|error| admission(error.to_string()))?;
            which::which_in(&argv[0], Some(search), &cwd).map_err(|error| {
                admission(format!(
                    "command executable is not found in PATH: {}: {error}",
                    argv[0]
                ))
            })?
        };
        // Match abspath without dereferencing a venv or another executable symlink.
        let mut absolute = PathBuf::new();
        for component in executable.components() {
            match component {
                PathComponent::ParentDir => {
                    absolute.pop();
                }
                PathComponent::CurDir => (),
                value => absolute.push(value),
            }
        }
        which::which(&absolute)
            .map_err(|_| admission("command executable must be an executable regular file"))?;
        argv[0] = absolute
            .into_os_string()
            .into_string()
            .map_err(|_| admission("command executable path must be UTF-8"))?;
        if argv[0].len() > 4096 || argv.iter().map(|arg| arg.len() + 1).sum::<usize>() > 16 * 1024 {
            return Err(admission(
                "command exceeds 4096 bytes per argument or 16 KiB including argument terminators",
            ));
        }
        Ok(Self { name, argv, cwd })
    }
}

#[derive(Clone, Debug)]
pub struct HostProcessesOptions {
    pub commands: Vec<HostCommand>,
    pub allow_launch: bool,
}
impl HostProcessesOptions {
    fn validate(&self) -> Result<()> {
        let mut names = BTreeSet::new();
        if !(1..=8).contains(&self.commands.len())
            || self
                .commands
                .iter()
                .any(|command| !names.insert(&command.name))
        {
            return Err(admission(
                "app.host-processes requires 1..8 uniquely named commands",
            ));
        }
        Ok(())
    }
}

struct HostChild {
    label: String,
    process: Child,
    pidfd: Option<OwnedFd>,
    tails: [Vec<u8>; 2],
    eof: [bool; 2],
}
impl HostChild {
    fn prepare(&mut self) -> Result<()> {
        self.pidfd = Some(
            pidfd_open(Pid::from_child(&self.process), PidfdFlags::empty())
                .map_err(|error| failed(format!("host process {} pidfd: {error}", self.label)))?,
        );
        rustix::io::ioctl_fionbio(self.process.stdout.as_ref().unwrap(), true)
            .and_then(|_| rustix::io::ioctl_fionbio(self.process.stderr.as_ref().unwrap(), true))
            .map_err(|error| failed(format!("host process {} pipes: {error}", self.label)))
    }
    fn diagnostics(&self) -> [String; 2] {
        self.tails
            .each_ref()
            .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
    }
    fn pump(&mut self) -> Result<bool> {
        let mut changed = false;
        for index in 0..2 {
            if self.eof[index] {
                continue;
            }
            let reader: &mut dyn Read = if index == 0 {
                self.process.stdout.as_mut().unwrap()
            } else {
                self.process.stderr.as_mut().unwrap()
            };
            let mut buffer = [0; 4096];
            match reader.read(&mut buffer) {
                Ok(0) => self.eof[index] = true,
                Ok(count) => {
                    let tail = &mut self.tails[index];
                    let previous = tail.clone();
                    let excess = (tail.len() + count).saturating_sub(12_000);
                    tail.drain(..excess);
                    tail.extend_from_slice(&buffer[..count]);
                    changed |= *tail != previous;
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                Err(error) => {
                    return Err(failed(format!("host process {} pipe: {error}", self.label)))
                }
            }
        }
        Ok(changed)
    }
}

#[derive(Default)]
struct State {
    children: Vec<HostChild>,
    started: bool,
    closed: bool,
}
impl State {
    fn check(&self) -> Result<()> {
        if !self.closed {
            for child in &self.children {
                if let Some(status) =
                    exit_status(Pid::from_child(&child.process)).map_err(|error| {
                        failed(format!(
                            "host process {} exit observation: {error}",
                            child.label
                        ))
                    })?
                {
                    let [stdout, stderr] = child.diagnostics();
                    let code = if status.exited() {
                        status.exit_status().unwrap()
                    } else {
                        -status.terminating_signal().unwrap()
                    };
                    return Err(failed(format!(
                        "host process {} exited ({code}): stdout={stdout:?}, stderr={stderr:?}",
                        child.label
                    )));
                }
            }
        }
        Ok(())
    }
    fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        let mut errors = Vec::new();
        for child in self.children.iter_mut().rev() {
            let (_, child_errors) =
                stop_child_group(&mut child.process, child.pidfd.as_ref().map(AsFd::as_fd));
            errors.extend(
                child_errors
                    .into_iter()
                    .map(|error| format!("host process {}: {error}", child.label)),
            );
            child.process.stdout.take();
            child.process.stderr.take();
            child.pidfd.take();
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(failed(errors.join("; cleanup: ")))
        }
    }
}

pub struct HostProcesses {
    options: HostProcessesOptions,
    state: Rc<RefCell<State>>,
}
impl Sampler<HostInput, HostEvent> for HostProcesses {
    fn sample(&mut self, _: u64, context: &HostContext) -> Result<()> {
        let count = self.state.borrow().children.len();
        for index in 0..count {
            context.check_deadline()?;
            let update = {
                let mut state = self.state.borrow_mut();
                let child = &mut state.children[index];
                if child.pump()? {
                    Some((child.label.clone(), child.diagnostics()))
                } else {
                    None
                }
            };
            if let Some((name, [stdout, stderr])) = update {
                context.emit(
                    None,
                    "process.output",
                    HostEvent::ProcessOutput {
                        name,
                        stdout,
                        stderr,
                    },
                    EventTime::default(),
                )?;
            }
        }
        self.state.borrow().check()
    }
}
impl Component<HostInput, HostEvent> for HostProcesses {
    fn start(&mut self, context: &HostContext) -> Result<()> {
        {
            let mut state = self.state.borrow_mut();
            if state.started || state.closed {
                return Err(failed("host programs can only start once"));
            }
            state.started = true;
        }
        for command in &self.options.commands {
            context.check_deadline()?;
            let process = Command::new(&command.argv[0])
                .args(&command.argv[1..])
                .current_dir(&command.cwd)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .process_group(0)
                .spawn()
                .map_err(|error| failed(format!("host process {} spawn: {error}", command.name)))?;
            let pid = process.id();
            // Register the child before any fallible pipe or pidfd setup.
            {
                let mut state = self.state.borrow_mut();
                state.children.push(HostChild {
                    label: command.name.clone(),
                    process,
                    pidfd: None,
                    tails: Default::default(),
                    eof: [false; 2],
                });
                state.children.last_mut().unwrap().prepare()?;
            }
            context.emit(
                None,
                "process.started",
                HostEvent::ProcessStarted {
                    name: command.name.clone(),
                    pid,
                },
                EventTime::default(),
            )?;
            context.check_deadline()?;
        }
        Ok(())
    }
    fn close(&mut self) -> Result<()> {
        self.state.borrow_mut().close()
    }
    fn sampler(&mut self) -> Option<&mut dyn Sampler<HostInput, HostEvent>> {
        Some(self)
    }
    fn health_check(&self) -> Option<HealthCheck> {
        let state = Rc::downgrade(&self.state);
        Some(Rc::new(move || {
            state
                .upgrade()
                .map_or(Ok(()), |state| state.borrow().check())
        }))
    }
}
impl Drop for HostProcesses {
    fn drop(&mut self) {
        let _ = self.state.borrow_mut().close();
    }
}

pub struct HostProcessesFactory;
impl Factory<HostOptions, HostInput, HostEvent> for HostProcessesFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::HostProcesses(options) = &node.options else {
            return Err(admission("app.host-processes requires host process options"));
        };
        options.validate()?;
        Ok(NodeDescription::new(
            vec![],
            [Capability::Health, Capability::Sample],
        ))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::HostProcesses(options) = &node.options else {
            return Err(admission("app.host-processes requires host process options"));
        };
        options.validate()?;
        if !options.allow_launch {
            return Err(admission(
                "host processes require explicit --allow-host-processes or factory opt-in",
            ));
        }
        Ok(Box::new(HostProcesses {
            options: options.clone(),
            state: Default::default(),
        }))
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        _: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        let count: usize = topology
            .nodes
            .iter()
            .map(|node| match &node.options {
                HostOptions::HostProcesses(options) => options.commands.len(),
                _ => 0,
            })
            .sum();
        if count > 8 {
            return Err(admission("a simulation permits at most 8 host processes"));
        }
        Ok(())
    }
}
