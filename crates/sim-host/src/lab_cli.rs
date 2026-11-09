use crate::cli_support::{error_code, seconds, Signals};
use clap::{Args, Subcommand};
use sim_host::{
    lab::{interactive::run_interactive, LabOptions},
    launch::LaunchError,
};
use std::{
    io::{self, Write},
    os::fd::AsFd,
    path::PathBuf,
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
}

#[cfg(feature = "web")]
#[derive(Args)]
struct WebOptions {
    #[arg(long)]
    elf: Option<PathBuf>,
    #[arg(long, default_value = "8765", value_parser = clap::value_parser!(u16).range(1..))]
    port: u16,
    #[arg(long)]
    qemu: Option<PathBuf>,
    #[arg(long, default_value = "3600", value_parser = seconds)]
    timeout: Duration,
    #[arg(long, default_value = "10", value_parser = seconds)]
    startup_timeout: Duration,
    #[arg(long)]
    qmp: Option<PathBuf>,
    #[arg(long, value_parser = clap::value_parser!(u16).range(1..))]
    gdb_port: Option<u16>,
}
#[derive(Args)]
struct InteractiveOptions {
    #[arg(long)]
    elf: PathBuf,
    #[arg(long = "uart", value_parser = ["uart1", "uart2", "uart3", "uart5", "uart7", "uart10"])]
    uarts: Vec<String>,
    #[arg(long = "can", value_parser = ["can1", "can2", "can3"])]
    cans: Vec<String>,
    #[arg(long)]
    adc: bool,
    #[arg(long)]
    qemu: Option<PathBuf>,
    /// Active host deadline including startup; cleanup has separate budgets.
    #[arg(long, default_value = "300", value_parser = seconds)]
    timeout: Duration,
    #[arg(long, default_value = "10", value_parser = seconds)]
    startup_timeout: Duration,
    #[arg(long)]
    qmp: Option<PathBuf>,
    #[arg(long, value_parser = clap::value_parser!(u16).range(1..))]
    gdb_port: Option<u16>,
    /// Start paused for native GDB (requires --gdb-port).
    #[arg(long, requires = "gdb_port")]
    paused: bool,
}

pub fn run(options: Options) -> Result<i32, LaunchError> {
    match options.command {
        Command::Interactive(args) => interactive(args),
        #[cfg(feature = "web")]
        Command::Web(args) => {
            let signals = Signals::new().map_err(|error| LaunchError {
                code: 1,
                message: error.to_string(),
            })?;
            sim_host::lab::web::run_web(
                sim_host::lab::session::SessionOptions {
                    firmware: args.elf,
                    qemu: args.qemu,
                    timeout: args.timeout,
                    startup_timeout: args.startup_timeout,
                    qmp_path: args.qmp,
                    gdb_port: args.gdb_port,
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
    }
}
fn interactive(args: InteractiveOptions) -> Result<i32, LaunchError> {
    let signals = Signals::new().map_err(|error| LaunchError {
        code: 1,
        message: error.to_string(),
    })?;
    let mut options = LabOptions::new(args.elf);
    if !args.uarts.is_empty() {
        options.uarts = args.uarts;
    }
    options.cans = args.cans;
    options.adc = args.adc;
    options.qemu = args.qemu;
    options.timeout = args.timeout;
    options.startup_timeout = args.startup_timeout;
    options.qmp_path = args.qmp;
    options.gdb_port = args.gdb_port;
    options.cancellation = Some(signals.cancelled.clone());
    let input = io::stdin();
    let mut output = io::stdout().lock();
    let result =
        run_interactive(options, input.as_fd(), &mut output, args.paused).map_err(|error| {
            let code = error_code(&error.primary);
            let mut message = error.to_string();
            for note in error.notes {
                message.push_str(&format!("\n{note}"));
            }
            LaunchError { code, message }
        })?;
    writeln!(
        output,
        "Lab finished: {} commands, {} observation batches, end_ns={}",
        result.commands, result.observation_batches, result.end_ns
    )
    .map_err(|error| LaunchError {
        code: 1,
        message: error.to_string(),
    })?;
    Ok(0)
}
