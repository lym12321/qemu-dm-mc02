use clap::{Args, ValueEnum};
use sim_host::launch::{
    check_machine, path_option, readable_file, resolve_qemu, socket_path, LaunchError,
};
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use sim_host::board_launch::SERIAL_SLOTS;

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Uart {
    Uart1,
    Uart2,
    Uart3,
    Uart5,
    Uart7,
    Uart10,
    None,
}

impl Uart {
    fn name(self) -> &'static str {
        match self {
            Self::Uart1 => "uart1",
            Self::Uart2 => "uart2",
            Self::Uart3 => "uart3",
            Self::Uart5 => "uart5",
            Self::Uart7 => "uart7",
            Self::Uart10 => "uart10",
            Self::None => "none",
        }
    }
}

#[derive(Args)]
pub struct Options {
    /// DM-MC02 firmware ELF.
    #[arg(value_name = "ELF")]
    elf: PathBuf,
    /// Project QEMU executable (or QEMU_SYSTEM_ARM).
    #[arg(long, value_name = "PATH")]
    qemu: Option<PathBuf>,
    /// Terminal UART; UART5 is RX only.
    #[arg(long, value_enum, default_value = "uart1")]
    uart: Uart,
    /// Pause at reset and enable native GDB.
    #[arg(long)]
    debug: bool,
    /// GDB port with --debug (default: 1234).
    #[arg(long, requires = "debug", value_parser = clap::value_parser!(u16).range(1..))]
    gdb_port: Option<u16>,
    /// dm-mc02, optionally with machine properties.
    #[arg(long, default_value = "dm-mc02")]
    machine: String,
    /// Additional native QMP Unix socket.
    #[arg(long, value_name = "PATH")]
    qmp: Option<PathBuf>,
    /// Co-sim Unix socket for a separately launched worker.
    #[arg(long, value_name = "PATH")]
    cosim: Option<PathBuf>,
}

fn command_arguments(
    options: &Options,
    elf: &Path,
    qmp: Option<&Path>,
    cosim: Option<&Path>,
) -> Vec<OsString> {
    let mut args: Vec<OsString> = ["-machine", &options.machine, "-kernel"]
        .into_iter()
        .map(Into::into)
        .collect();
    args.push(elf.as_os_str().to_owned());
    args.extend(
        [
            "-nodefaults",
            "-display",
            "none",
            "-monitor",
            if options.uart == Uart::None {
                "stdio"
            } else {
                "none"
            },
        ]
        .into_iter()
        .map(Into::into),
    );
    for &slot in SERIAL_SLOTS {
        if slot == options.uart.name() {
            args.extend(["-serial", "mon:stdio"].into_iter().map(Into::into));
        } else if slot == "cosim" && cosim.is_some() {
            args.push("-chardev".into());
            args.push(path_option(
                "socket,id=cosim,path=",
                cosim.unwrap(),
                ",server=on,wait=off",
            ));
            args.extend(["-serial", "chardev:cosim"].into_iter().map(Into::into));
        } else {
            args.extend(["-serial", "none"].into_iter().map(Into::into));
        }
    }
    if let Some(path) = qmp {
        args.push("-qmp".into());
        args.push(path_option("unix:", path, ",server=on,wait=off"));
    }
    if options.debug {
        args.extend([
            "-S".into(),
            "-gdb".into(),
            format!("tcp:127.0.0.1:{}", options.gdb_port.unwrap_or(1234)).into(),
        ]);
    }
    args
}

pub fn run(options: Options) -> Result<i32, LaunchError> {
    let elf = readable_file(&options.elf, "firmware ELF")?;
    if options.machine != "dm-mc02" && !options.machine.starts_with("dm-mc02,") {
        return Err(LaunchError::admission(
            "firmware requires --machine dm-mc02, optionally followed by machine properties",
        ));
    }
    let qmp = socket_path(options.qmp.as_deref())?;
    let cosim = socket_path(options.cosim.as_deref())?;
    if qmp.is_some() && qmp == cosim {
        return Err(LaunchError::admission(
            "QMP and co-sim socket paths must differ",
        ));
    }
    let qemu = resolve_qemu(options.qemu.clone())?;
    check_machine(&qemu)?;
    let args = command_arguments(&options, &elf, qmp.as_deref(), cosim.as_deref());
    eprintln!(
        "QEMU: {}\nELF: {}\nMachine: {}\nUART: {}",
        qemu.display(),
        elf.display(),
        options.machine,
        options.uart.name()
    );
    eprintln!(
        "{}",
        if options.uart == Uart::None {
            "QEMU monitor: type quit to exit."
        } else {
            "Ctrl-a c: switch UART/monitor; Ctrl-a x: quit; Ctrl-a h: help."
        }
    );
    if let Some(path) = cosim {
        eprintln!(
            "Co-sim socket: {} (external worker required)",
            path.display()
        );
    }
    if let Some(path) = qmp {
        eprintln!("QMP socket: {}", path.display());
    }
    if options.debug {
        eprintln!(
            "Paused at reset. In GDB: target remote 127.0.0.1:{}",
            options.gdb_port.unwrap_or(1234)
        );
    }
    std::io::stderr().flush().map_err(|error| {
        LaunchError::admission(format!("cannot write launch diagnostics: {error}"))
    })?;
    let mut command = Command::new(&qemu);
    command.args(args);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let error = command.exec();
        Err(LaunchError::admission(format!(
            "cannot execute QEMU: {error}"
        )))
    }
    #[cfg(windows)]
    {
        let status = command
            .status()
            .map_err(|error| LaunchError::admission(format!("cannot execute QEMU: {error}")))?;
        Ok(status.code().unwrap_or(1))
    }
}
