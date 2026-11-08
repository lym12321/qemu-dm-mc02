mod firmware;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "sim",
    version,
    about = "Run DM-MC02 firmware with native QEMU."
)]
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
}

fn main() {
    let result = match Cli::parse().command {
        Command::Firmware(options) => firmware::run(options),
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
