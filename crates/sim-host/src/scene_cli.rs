use crate::cli_support::{error_code, error_type, seconds, Signals};
use clap::Args;
use serde_json::{json, Value};
use sim_core::{Observer, RunMode, SimulationOptions, StepStatus};
use sim_host::launch::LaunchError;
use sim_host::{
    apps::experiment::run_experiment,
    config::scene::{load_scene, Scene},
    event_json::{event_json, result_json},
    runtime::{HostEvent, HostInput},
};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Component, Path, PathBuf},
    rc::Rc,
    sync::Arc,
    time::Duration,
};

#[derive(Args)]
pub struct ValidateOptions {
    scene: PathBuf,
}

#[derive(Args)]
pub struct RunOptions {
    scene: PathBuf,
    /// Total host budget including startup, in seconds.
    #[arg(long, value_parser = seconds)]
    timeout: Option<Duration>,
    /// Startup budget, in seconds.
    #[arg(long, default_value = "10", value_parser = seconds)]
    startup_timeout: Duration,
    /// Create a new JSON report after cleanup.
    #[arg(long)]
    report: Option<PathBuf>,
    /// Permit the scene's admitted literal host commands to run.
    #[cfg(all(target_os = "linux", feature = "host-processes"))]
    #[arg(long)]
    allow_host_processes: bool,
    /// Record a finite stepped experiment to a new MCAP file.
    #[cfg(feature = "trace")]
    #[arg(long)]
    trace: Option<PathBuf>,
    /// Native QEMU executable (or QEMU_SYSTEM_ARM).
    #[cfg(all(target_os = "linux", feature = "machine"))]
    #[arg(long)]
    qemu: Option<PathBuf>,
    /// Checkout containing the default native executable.
    #[cfg(all(target_os = "linux", feature = "machine"))]
    #[arg(long)]
    backend_root: Option<PathBuf>,
    /// Single-machine native GDB port.
    #[cfg(all(target_os = "linux", feature = "machine"))]
    #[arg(long, value_parser = clap::value_parser!(u16).range(1..))]
    gdb_port: Option<u16>,
    /// Single-machine additional native QMP socket.
    #[cfg(all(target_os = "linux", feature = "machine"))]
    #[arg(long)]
    qmp: Option<PathBuf>,
}

#[cfg(feature = "trace")]
#[derive(Args)]
pub struct ReplayOptions {
    trace: PathBuf,
    /// Bounded YAML replacement assertions.
    #[arg(long)]
    assertions: Option<PathBuf>,
    /// Create a new offline JSON report after cleanup.
    #[arg(long)]
    report: Option<PathBuf>,
}

fn failure(error: impl std::fmt::Display) -> LaunchError {
    LaunchError {
        code: 1,
        message: error.to_string(),
    }
}

fn observed_error(error: impl std::error::Error + Send + Sync + 'static) -> sim_core::Failure {
    Arc::new(error)
}

pub fn validate(options: ValidateOptions) -> Result<i32, LaunchError> {
    let scene = load_scene(&options.scene).map_err(failure)?;
    println!(
        "valid: {} ({} nodes)",
        options.scene.display(),
        scene.topology.nodes.len()
    );
    Ok(0)
}

// Canonicalize existing ancestors, including symlinks, before comparing new
// outputs. The exclusive open below remains the final authority on creation.
fn destination(path: &Path) -> io::Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut resolved = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::ParentDir => {
                resolved.pop();
            }
            Component::CurDir => (),
            component => {
                resolved.push(component);
                if resolved.try_exists()? {
                    resolved = fs::canonicalize(&resolved)?;
                }
            }
        }
    }
    Ok(resolved)
}

fn admit_output(path: &Path, inputs: &[&Path]) -> Result<(), LaunchError> {
    if path.symlink_metadata().is_ok() {
        return Err(failure(format!(
            "output already exists: {}",
            path.display()
        )));
    }
    let output = destination(path).map_err(failure)?;
    for input in inputs {
        let input = destination(input).map_err(failure)?;
        if output.starts_with(&input) || input.starts_with(&output) {
            return Err(failure(
                "output paths must differ from input paths and cannot contain one another",
            ));
        }
    }
    Ok(())
}

fn emit() -> Observer<HostInput, HostEvent> {
    Rc::new(|event, _| {
        if event.kind != "snapshot" {
            let mut output = io::stdout().lock();
            serde_json::to_writer(&mut output, &event_json(event)).map_err(observed_error)?;
            writeln!(output).map_err(observed_error)?;
            output.flush().map_err(observed_error)?;
        }
        Ok(())
    })
}

fn run_plain(
    scene: &Scene,
    options: SimulationOptions,
    observer: Observer<HostInput, HostEvent>,
) -> Result<Value, (i32, Value)> {
    let mut owner = scene.simulation(options).map_err(|error| {
        (
            error_code(&error),
            json!({"error":error.to_string(),"error_type":error_type(&error),"notes":[]}),
        )
    })?;
    owner.set_observer(Some(observer));
    let execution = (|| {
        owner.start(None)?;
        let result = owner.run_until(scene.duration_ns, None)?;
        if result.status != StepStatus::Ok || result.reached_time_ns != scene.duration_ns {
            return Err(std::sync::Arc::new(sim_core::Error::State(
                "scene did not complete its requested duration".into(),
            )) as sim_core::Failure);
        }
        Ok(())
    })();
    let mut primary = execution.err();
    let mut notes = Vec::new();
    if let Err(error) = owner.close() {
        if let Some(first) = &primary {
            if !std::sync::Arc::ptr_eq(first, &error) {
                notes.push(format!("Simulation cleanup: {error}"));
            }
        } else {
            primary = Some(error);
        }
    }
    notes.extend(owner.failure_notes());
    if let Some(error) = primary {
        Err((
            error_code(&error),
            json!({"error":error.to_string(),"error_type":error_type(&error),"notes":notes,"time_ns":owner.t_sim_ns(),"events_total":owner.events_total()}),
        ))
    } else {
        Ok(json!({"time_ns":owner.t_sim_ns()}))
    }
}

pub fn run(options: RunOptions) -> Result<i32, LaunchError> {
    #[cfg(feature = "trace")]
    if let Some(trace) = &options.trace {
        let mut inputs = vec![options.scene.as_path()];
        if let Some(report) = &options.report {
            inputs.push(report);
        }
        admit_output(trace, &inputs)?;
    }
    if let Some(report) = &options.report {
        admit_output(report, &[&options.scene])?;
    }
    let signals = Signals::new().map_err(failure)?;
    let execution = (|| {
        let scene = load_scene(&options.scene).map_err(failure)?;
        #[cfg(all(target_os = "linux", feature = "host-processes"))]
        let scene = {
            let mut scene = scene;
            for node in &mut scene.topology.nodes {
                if let sim_host::runtime::HostOptions::HostProcesses(processes) = &mut node.options
                {
                    processes.allow_launch = options.allow_host_processes;
                }
            }
            scene
        };
        #[cfg(all(target_os = "linux", feature = "machine"))]
        let scene = {
            use sim_host::runtime::HostOptions;
            let mut scene = scene;
            let machines = scene
                .topology
                .nodes
                .iter()
                .filter(|node| matches!(node.options, HostOptions::Qemu(_)))
                .count();
            if (options.gdb_port.is_some() || options.qmp.is_some()) && machines != 1 {
                return Err(failure("--gdb-port/--qmp require exactly one QEMU machine"));
            }
            for node in &mut scene.topology.nodes {
                if let HostOptions::Qemu(machine) = &mut node.options {
                    machine.qemu = options.qemu.clone();
                    machine.backend_root = options.backend_root.clone();
                    machine.gdb_port = options.gdb_port;
                    machine.qmp_path = options.qmp.clone();
                }
            }
            scene
        };
        if scene.mode != RunMode::Stepped {
            return Err(failure(
                "interactive scenes require the interactive command consumer",
            ));
        }
        let simulation = SimulationOptions {
            startup_timeout: options.startup_timeout,
            host_timeout: options.timeout,
            cancellation: Some(signals.cancelled.clone()),
            ..SimulationOptions::default()
        };
        let recording = {
            #[cfg(feature = "trace")]
            {
                options.trace.is_some()
            }
            #[cfg(not(feature = "trace"))]
            {
                false
            }
        };
        if scene.experiment.is_some() || recording {
            let execution = {
                #[cfg(feature = "trace")]
                {
                    if let Some(path) = &options.trace {
                        sim_host::apps::recording::run_recorded(
                            &scene,
                            simulation,
                            create_output(path)?,
                            Some(emit()),
                        )
                    } else {
                        run_experiment(&scene, simulation, Some(emit()))
                    }
                }
                #[cfg(not(feature = "trace"))]
                {
                    run_experiment(&scene, simulation, Some(emit()))
                }
            };
            match execution {
                Ok(result) => {
                    for error in &result.failures {
                        eprintln!("assertion failed: {error}");
                    }
                    let code = i32::from(!result.passed());
                    Ok((
                        code,
                        json!({"status":if code==0 {"passed"} else {"failed"},"result":if scene.experiment.is_some() { result_json(&result) } else { json!({"time_ns":result.end_ns}) }}),
                    ))
                }
                Err(error) => {
                    let code = error_code(&error.primary);
                    Ok((
                        code,
                        json!({"status":if code==78 {"blocked"} else {"failed"},"error":error.primary.to_string(),"error_type":error_type(&error.primary),"notes":error.notes,"time_ns":error.time_ns,"events_total":error.events_total}),
                    ))
                }
            }
        } else {
            Ok(match run_plain(&scene, simulation, emit()) {
                Ok(result) => (0, json!({"status":"passed","result":result})),
                Err((code, mut payload)) => {
                    payload["status"] = json!(if code == 78 { "blocked" } else { "failed" });
                    (code, payload)
                }
            })
        }
    })();
    let (code, payload) = match execution {
        Ok(result) => result,
        Err(error) => (
            error.code,
            json!({"status":"failed","error":error.message,"error_type":"RuntimeError","notes":[]}),
        ),
    };
    publish(code, payload, options.report.as_deref(), "")
}

fn create_output(path: &Path) -> Result<File, LaunchError> {
    if let Some(parent) = path.parent().filter(|v| !v.as_os_str().is_empty()) {
        fs::create_dir_all(parent).map_err(failure)?;
    }
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(failure)
}

fn publish(
    code: i32,
    payload: Value,
    report: Option<&Path>,
    prefix: &str,
) -> Result<i32, LaunchError> {
    let report_result = (|| {
        if let Some(path) = report {
            let mut report = create_output(path)?;
            serde_json::to_writer_pretty(&mut report, &payload).map_err(failure)?;
            writeln!(report).map_err(failure)?;
            report.flush().map_err(failure)?;
        }
        Ok(())
    })();
    let report_error = match report_result {
        Ok(()) => None,
        Err(error) if payload.get("error").is_some() => Some(error),
        Err(error) => return Err(error),
    };
    if let Some(error) = payload.get("error").and_then(Value::as_str) {
        eprintln!("error: {error}");
        if let Some(notes) = payload.get("notes").and_then(Value::as_array) {
            for note in notes {
                eprintln!("{}", note.as_str().unwrap_or(""));
            }
        }
        if let Some(error) = report_error {
            eprintln!("report: {}", error.message);
        }
    } else {
        let result = &payload["result"];
        println!(
            "{prefix}{}: time_ns={}",
            payload["status"].as_str().unwrap(),
            result
                .get("time_ns")
                .or_else(|| result.get("end_ns"))
                .unwrap()
        );
    }
    Ok(code)
}

#[cfg(feature = "trace")]
pub fn replay(options: ReplayOptions) -> Result<i32, LaunchError> {
    if let Some(report) = &options.report {
        let mut inputs = vec![options.trace.as_path()];
        if let Some(assertions) = &options.assertions {
            inputs.push(assertions);
        }
        admit_output(report, &inputs)?;
    }
    let provenance = json!({"mode":"offline-replay","trace":destination(&options.trace).map_err(failure)?,
        "assertions":options.assertions.as_ref().map(|v| destination(v)).transpose().map_err(failure)?});
    let execution = (|| {
        let replacement = options
            .assertions
            .as_ref()
            .map(sim_host::config::yaml::load_document)
            .transpose()
            .map_err(observed_error)?;
        if !options.trace.is_file() {
            return Err(observed_error(io::Error::other(
                "replay trace must be a regular file",
            )));
        }
        let mut source = File::open(&options.trace).map_err(observed_error)?;
        sim_host::apps::recording::replay_experiment(
            &mut source,
            replacement.as_ref().map(|(_, value)| value),
        )
    })();
    let (code, mut payload) = match execution {
        Ok(result) => {
            for error in &result.failures {
                eprintln!("assertion failed: {error}");
            }
            let code = i32::from(!result.passed());
            (
                code,
                json!({"status":if code==0 {"passed"} else {"failed"},"result":result_json(&result)}),
            )
        }
        Err(error) => (
            error_code(&error),
            json!({"status":"failed","error":error.to_string(),"error_type":error_type(&error),"notes":[]}),
        ),
    };
    payload["provenance"] = provenance;
    publish(code, payload, options.report.as_deref(), "offline replay ")
}
