use crate::cli_support::{Signals, error_code, seconds};
use clap::{Args, Subcommand, ValueEnum};
use sim_host::{
    lab::{
        ImuMode, StartupMode,
        interactive::run_interactive_with_project,
        project::{LabProject, ProjectFile},
    },
    launch::LaunchError,
};
#[cfg(unix)]
use std::os::fd::AsFd;
#[cfg(windows)]
use std::os::windows::io::AsHandle;
use std::{
    io::{self, Write},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

#[derive(Args)]
pub struct Options {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Observe native UART/CAN and enter public board IO commands.
    Interactive(InteractiveOptions),
    /// Start the local browser board workbench.
    #[cfg(feature = "web")]
    Web(WebOptions),
    /// Print a native debugger attach configuration.
    Ide(IdeOptions),
}

#[derive(Clone, Copy, ValueEnum)]
enum Startup {
    Run,
    WaitGdb,
    User,
}
impl From<Startup> for StartupMode {
    fn from(value: Startup) -> Self {
        match value {
            Startup::Run => Self::Run,
            Startup::WaitGdb => Self::WaitGdb,
            Startup::User => Self::User,
        }
    }
}
#[derive(Clone, Copy, ValueEnum)]
enum Imu {
    Off,
    Stationary,
}
impl From<Imu> for ImuMode {
    fn from(value: Imu) -> Self {
        match value {
            Imu::Off => Self::Off,
            Imu::Stationary => Self::Stationary,
        }
    }
}
#[derive(Args, Default)]
struct LaunchOptions {
    #[arg(long)]
    project: Option<PathBuf>,
    #[arg(long)]
    elf: Option<PathBuf>,
    #[arg(long, value_enum)]
    startup: Option<Startup>,
    #[arg(long, value_parser = clap::value_parser!(u64).range(1..))]
    user_hold_ms: Option<u64>,
    #[arg(long = "uart", value_parser = ["uart1", "uart2", "uart3", "uart5", "uart7", "uart10"])]
    uarts: Vec<String>,
    #[arg(long = "can", value_parser = ["can1", "can2", "can3"])]
    cans: Vec<String>,
    #[arg(long, num_args=0..=1, default_missing_value="true", value_parser=clap::value_parser!(bool))]
    adc: Option<bool>,
    /// IMU input fixture: a stationary board or no supplied samples.
    #[arg(long, value_enum)]
    imu: Option<Imu>,
    #[arg(long)]
    qemu: Option<PathBuf>,
    /// Active host deadline including startup; cleanup has separate budgets.
    #[arg(long, value_parser = seconds)]
    timeout: Option<Duration>,
    #[arg(long, value_parser = seconds)]
    startup_timeout: Option<Duration>,
    #[arg(long)]
    qmp: Option<PathBuf>,
    #[arg(long, value_parser = clap::value_parser!(u16).range(1..), conflicts_with="no_gdb")]
    gdb_port: Option<u16>,
    #[arg(long)]
    no_gdb: bool,
    /// Export the guest USB device over loopback USB/IP (default 127.0.0.1:3240).
    #[arg(long, num_args=0..=1, default_missing_value="127.0.0.1:3240", conflicts_with="no_usbip")]
    usbip: Option<std::net::SocketAddr>,
    #[arg(long)]
    no_usbip: bool,
}
#[cfg(feature = "web")]
#[derive(Args)]
struct WebOptions {
    #[command(flatten)]
    launch: LaunchOptions,
    /// Restart automatically when the local ELF changes.
    #[arg(long, conflicts_with = "no_watch")]
    watch: bool,
    /// Disable ELF watching saved in the project.
    #[arg(long)]
    no_watch: bool,
    #[arg(long, default_value = "8765", value_parser = clap::value_parser!(u16).range(1..))]
    port: u16,
}
#[cfg(feature = "web")]
impl WebOptions {
    fn project(&self) -> Result<(LabProject, Option<Arc<ProjectFile>>), LaunchError> {
        let (mut project, file) = merged(&self.launch, false, false)?;
        if self.watch {
            project.watch_elf = true;
        }
        if self.no_watch {
            project.watch_elf = false;
        }
        project.validate().map_err(admission)?;
        Ok((project, file))
    }
}
#[derive(Args)]
struct InteractiveOptions {
    #[command(flatten)]
    launch: LaunchOptions,
    /// Start paused for native GDB (requires an enabled GDB port).
    #[arg(long, conflicts_with = "startup")]
    paused: bool,
}
#[derive(Clone, Copy, ValueEnum)]
enum IdeFormat {
    Vscode,
    Gdb,
    Clion,
}
#[derive(Args)]
struct IdeOptions {
    #[command(flatten)]
    launch: LaunchOptions,
    #[arg(long, value_enum, default_value = "vscode")]
    format: IdeFormat,
}

fn admission(error: impl std::fmt::Display) -> LaunchError {
    LaunchError::admission(error.to_string())
}
fn merged(
    args: &LaunchOptions,
    legacy_interactive: bool,
    paused: bool,
) -> Result<(LabProject, Option<Arc<ProjectFile>>), LaunchError> {
    let file = args
        .project
        .as_ref()
        .map(ProjectFile::open)
        .transpose()
        .map_err(admission)?
        .map(Arc::new);
    let mut project = match &file {
        Some(file) => file.resolve(file.project()).map_err(admission)?,
        None => LabProject::default(),
    };
    if legacy_interactive && file.is_none() {
        project.uarts = vec!["uart1".into()];
        project.cans.clear();
        project.adc = false;
        project.timeout_secs = 300.0;
    }
    if let Some(elf) = &args.elf {
        project.firmware = Some(if elf.is_absolute() {
            elf.clone()
        } else {
            std::env::current_dir().map_err(admission)?.join(elf)
        });
    }
    if let Some(startup) = args.startup {
        project.startup = startup.into();
    }
    if paused {
        project.startup = StartupMode::WaitGdb;
    }
    if let Some(hold) = args.user_hold_ms {
        project.user_hold_ms = hold;
    }
    if !args.uarts.is_empty() {
        project.uarts = args.uarts.clone();
    }
    if !args.cans.is_empty() {
        project.cans = args.cans.clone();
    }
    if let Some(adc) = args.adc {
        project.adc = adc;
    }
    if let Some(imu) = args.imu {
        project.imu = imu.into();
    }
    if let Some(timeout) = args.timeout {
        project.timeout_secs = timeout.as_secs_f64();
    }
    if let Some(timeout) = args.startup_timeout {
        project.startup_timeout_secs = timeout.as_secs_f64();
    }
    if args.gdb_port.is_some() {
        project.gdb_port = args.gdb_port;
    }
    if args.no_gdb {
        project.gdb_port = None;
    }
    if args.usbip.is_some() {
        project.usbip = args.usbip;
    }
    if args.no_usbip {
        project.usbip = None;
    }
    project.validate().map_err(admission)?;
    Ok((project, file))
}

pub fn run(options: Options) -> Result<i32, LaunchError> {
    match options.command {
        Command::Interactive(args) => interactive(args),
        #[cfg(feature = "web")]
        Command::Web(args) => {
            let (project, file) = args.project()?;
            let signals = Signals::new().map_err(admission)?;
            sim_host::lab::web::run_web(
                sim_host::lab::session::SessionOptions {
                    firmware: project.firmware.clone(),
                    qemu: args.launch.qemu,
                    timeout: Duration::from_secs_f64(project.timeout_secs),
                    startup_timeout: Duration::from_secs_f64(project.startup_timeout_secs),
                    qmp_path: args.launch.qmp,
                    gdb_port: project.gdb_port,
                    usbip: project.usbip,
                    project: file,
                    launch: Some(project),
                },
                args.port,
                signals.cancelled.clone(),
            )
            .map_err(|error| LaunchError {
                code: error_code(&error.primary),
                message: std::iter::once(error.to_string())
                    .chain(error.notes)
                    .collect::<Vec<_>>()
                    .join("\n"),
            })?;
            Ok(0)
        }
        Command::Ide(args) => {
            let (project, _) = merged(&args.launch, false, false)?;
            let config = project.ide_config().map_err(admission)?;
            let mut out = io::stdout().lock();
            match args.format {
                IdeFormat::Gdb => out
                    .write_all(config["gdb"].as_str().expect("GDB text").as_bytes())
                    .map_err(admission)?,
                IdeFormat::Vscode | IdeFormat::Clion => {
                    let key = match args.format {
                        IdeFormat::Vscode => "vscode",
                        _ => "clion",
                    };
                    serde_json::to_writer_pretty(&mut out, &config[key]).map_err(admission)?;
                    writeln!(out).map_err(admission)?;
                }
            }
            Ok(0)
        }
    }
}
fn interactive(args: InteractiveOptions) -> Result<i32, LaunchError> {
    let (project, _) = merged(&args.launch, true, args.paused)?;
    if project.watch_elf {
        return Err(admission(
            "ELF watching requires lab web; set watch_elf to false for interactive",
        ));
    }
    let commands = project.commands().map_err(admission)?;
    let signals = Signals::new().map_err(admission)?;
    let options = project
        .lab_options(
            args.launch.qemu,
            args.launch.qmp,
            Some(signals.cancelled.clone()),
        )
        .map_err(admission)?;
    let input = io::stdin();
    #[cfg(unix)]
    let handle = input.as_fd();
    #[cfg(windows)]
    let handle = input.as_handle();
    let mut output = io::stdout().lock();
    let result = run_interactive_with_project(
        options,
        handle,
        &mut output,
        project.startup,
        Duration::from_millis(project.user_hold_ms),
        &commands,
    )
    .map_err(|error| LaunchError {
        code: error_code(&error.primary),
        message: std::iter::once(error.to_string())
            .chain(error.notes)
            .collect::<Vec<_>>()
            .join("\n"),
    })?;
    writeln!(
        output,
        "Lab finished: {} commands, {} observation batches, end_ns={}",
        result.commands, result.observation_batches, result.end_ns
    )
    .map_err(admission)?;
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;
    #[derive(Parser)]
    struct Cli {
        #[command(flatten)]
        options: InteractiveOptions,
    }

    #[test]
    fn project_settings_survive_defaults_and_explicit_flags_override() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sim.lab.json");
        let mut project = LabProject::default();
        project.firmware = Some("build/board.elf".into());
        project.timeout_secs = 45.0;
        project.startup_timeout_secs = 2.0;
        project.gdb_port = Some(5555);
        ProjectFile::open(&path).unwrap().save(&project).unwrap();
        let args = Cli::parse_from(["sim", "--project", path.to_str().unwrap()]).options;
        let (merged, _) = super::merged(&args.launch, true, args.paused).unwrap();
        assert_eq!(
            (
                merged.timeout_secs,
                merged.startup_timeout_secs,
                merged.gdb_port
            ),
            (45.0, 2.0, Some(5555))
        );
        assert_eq!(merged.firmware.unwrap(), dir.path().join("build/board.elf"));
        assert_eq!(merged.imu, ImuMode::Stationary);
        let args = Cli::parse_from([
            "sim",
            "--project",
            path.to_str().unwrap(),
            "--timeout",
            "60.5",
            "--no-gdb",
            "--adc=false",
            "--imu",
            "off",
            "--elf",
            "override.elf",
        ])
        .options;
        let (merged, _) = super::merged(&args.launch, true, args.paused).unwrap();
        assert_eq!(
            (merged.timeout_secs, merged.gdb_port, merged.adc),
            (60.5, None, false)
        );
        assert_eq!(merged.imu, ImuMode::Off);
        assert_eq!(
            merged.firmware.unwrap(),
            std::env::current_dir().unwrap().join("override.elf")
        );
    }
    #[test]
    fn legacy_interactive_defaults_and_project_gdb_paused_are_preserved() {
        let args = Cli::parse_from(["sim", "--elf", "board.elf"]).options;
        let (project, _) = merged(&args.launch, true, false).unwrap();
        assert_eq!(
            (
                project.uarts,
                project.cans,
                project.adc,
                project.timeout_secs
            ),
            (vec!["uart1".to_owned()], vec![], false, 300.0)
        );
        let args = Cli::parse_from([
            "sim",
            "--elf",
            "board.elf",
            "--gdb-port",
            "1234",
            "--paused",
        ])
        .options;
        assert_eq!(
            merged(&args.launch, true, args.paused).unwrap().0.startup,
            StartupMode::WaitGdb
        );
    }
    #[cfg(feature = "web")]
    #[test]
    fn web_watch_flags_override_project_and_interactive_rejects_watch() {
        #[derive(Parser)]
        struct WebCli {
            #[command(flatten)]
            options: WebOptions,
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sim.lab.json");
        let project = LabProject {
            firmware: Some("build/board.elf".into()),
            watch_elf: true,
            ..Default::default()
        };
        ProjectFile::open(&path).unwrap().save(&project).unwrap();
        for (extra, expected) in [(None, true), (Some("--no-watch"), false)] {
            let mut argv = vec!["sim", "--project", path.to_str().unwrap()];
            argv.extend(extra);
            assert_eq!(
                WebCli::parse_from(argv)
                    .options
                    .project()
                    .unwrap()
                    .0
                    .watch_elf,
                expected
            );
        }
        assert!(
            WebCli::parse_from(["sim", "--watch", "--elf", "board.elf"])
                .options
                .project()
                .unwrap()
                .0
                .watch_elf
        );
        assert!(WebCli::try_parse_from(["sim", "--watch", "--no-watch"]).is_err());
        let args = Cli::parse_from(["sim", "--project", path.to_str().unwrap()]).options;
        assert!(
            interactive(args)
                .unwrap_err()
                .to_string()
                .contains("requires lab web")
        );
    }
}
