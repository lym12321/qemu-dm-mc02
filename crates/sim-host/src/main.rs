#[cfg(any(
    feature = "scene",
    all(target_os = "linux", any(feature = "lab", feature = "worker"))
))]
mod cli_support;
mod firmware;
#[cfg(all(target_os = "linux", feature = "lab"))]
mod lab_cli;
#[cfg(feature = "scene")]
mod scene_cli;
#[cfg(all(target_os = "linux", feature = "worker"))]
mod worker_cli;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "sim", version, about = "Compose and run embedded simulations.")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run an ELF in the native QEMU terminal.
    #[command(
        long_about = "Run firmware directly; Ctrl-a c switches UART/monitor, Ctrl-a x quits."
    )]
    Firmware(firmware::Options),
    /// Free-running native board tools.
    #[cfg(all(target_os = "linux", feature = "lab"))]
    Lab(lab_cli::Options),
    /// Run the native co-simulation worker.
    #[cfg(all(target_os = "linux", feature = "worker"))]
    Worker(Box<worker_cli::Options>),
    /// Validate sim/v1 without starting native resources.
    #[cfg(feature = "scene")]
    Validate(scene_cli::ValidateOptions),
    /// Run a sim/v1 scene through Simulation.
    #[cfg(feature = "scene")]
    Run(scene_cli::RunOptions),
    /// Evaluate a recorded experiment offline.
    #[cfg(all(feature = "scene", feature = "trace"))]
    Replay(scene_cli::ReplayOptions),
}

fn main() {
    #[cfg(all(target_os = "linux", feature = "scene"))]
    if let Err(error) = sim_host::bundle::configure() {
        eprintln!("error: {}", error.message);
        std::process::exit(error.code);
    }
    let result = match Cli::parse().command {
        Command::Firmware(options) => firmware::run(options),
        #[cfg(all(target_os = "linux", feature = "lab"))]
        Command::Lab(options) => lab_cli::run(options),
        #[cfg(all(target_os = "linux", feature = "worker"))]
        Command::Worker(options) => worker_cli::run(*options),
        #[cfg(feature = "scene")]
        Command::Validate(options) => scene_cli::validate(options),
        #[cfg(feature = "scene")]
        Command::Run(options) => scene_cli::run(options),
        #[cfg(all(feature = "scene", feature = "trace"))]
        Command::Replay(options) => scene_cli::replay(options),
    };
    let code = match result {
        Ok(code) => code,
        Err(error) => {
            eprintln!("error: {}", error.message);
            error.code
        }
    };
    std::process::exit(code);
}
