use clap::{Args, ValueEnum};
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;
use wait_timeout::ChildExt;

const SERIAL_SLOTS: [&str; 10] = [
    "cosim", "uart1", "uart2", "uart3", "uart5", "uart7", "uart10", "can1", "can2", "can3",
];
const QUERY_TIMEOUT: Duration = Duration::from_secs(5);
const QUERY_OUTPUT_LIMIT: u64 = 128 * 1024;

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

#[derive(Debug)]
pub struct LaunchError {
    pub code: i32,
    pub message: String,
}

impl LaunchError {
    fn admission(message: impl Into<String>) -> Self {
        Self {
            code: 1,
            message: message.into(),
        }
    }

    fn backend(message: impl Into<String>) -> Self {
        Self {
            code: 78,
            message: message.into(),
        }
    }
}

fn readable_file(path: &Path, label: &str) -> Result<PathBuf, LaunchError> {
    let resolved = dunce::canonicalize(path).map_err(|error| {
        LaunchError::admission(format!(
            "{label} is not readable: {}: {error}",
            path.display()
        ))
    })?;
    if !resolved.is_file() || File::open(&resolved).is_err() {
        return Err(LaunchError::admission(format!(
            "{label} is not readable: {}",
            resolved.display()
        )));
    }
    Ok(resolved)
}

fn socket_path(path: Option<&Path>) -> Result<Option<PathBuf>, LaunchError> {
    let Some(path) = path else { return Ok(None) };
    let invalid = || {
        LaunchError::admission(
            "QMP socket needs an existing parent, a new path, and at most 100 bytes",
        )
    };
    // symlink_metadata also sees dangling symlinks, which must never be replaced.
    match fs::symlink_metadata(path) {
        Ok(_) => return Err(invalid()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(invalid()),
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let parent = dunce::canonicalize(parent).map_err(|_| invalid())?;
    let name = path.file_name().ok_or_else(invalid)?;
    let resolved = parent.join(name);
    if !parent.is_dir() || resolved.as_os_str().as_encoded_bytes().len() > 100 {
        return Err(invalid());
    }
    Ok(Some(resolved))
}

fn resolve_qemu(explicit: Option<PathBuf>) -> Result<PathBuf, LaunchError> {
    let candidate = explicit
        .filter(|path| !path.as_os_str().is_empty())
        .or_else(|| {
            std::env::var_os("QEMU_SYSTEM_ARM")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
        })
        .map(Ok)
        .unwrap_or_else(|| {
            std::env::current_exe().map(|executable| {
                executable
                    .parent()
                    .expect("current executable has a parent")
                    .join("bin")
                    .join(if cfg!(windows) {
                        "qemu-system-arm.exe"
                    } else {
                        "qemu-system-arm"
                    })
            })
        })
        .map_err(|error| LaunchError::backend(format!("cannot locate sim executable: {error}")))?;
    let found = which::which(&candidate)
        .ok()
        .and_then(|path| dunce::canonicalize(path).ok());
    found.ok_or_else(|| LaunchError::backend(format!(
        "QEMU is not executable: {}\nSelect a compatible binary with --qemu PATH or QEMU_SYSTEM_ARM.",
        candidate.display()
    )))
}

fn read_query_output(mut file: File) -> std::io::Result<String> {
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::new();
    file.take(QUERY_OUTPUT_LIMIT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > QUERY_OUTPUT_LIMIT {
        return Err(std::io::Error::other(
            "QEMU machine query output exceeds 128 KiB",
        ));
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn check_machine(qemu: &Path) -> Result<(), LaunchError> {
    let query_error = |error| {
        LaunchError::backend(format!(
            "cannot query QEMU machine support: {}: {error}",
            qemu.display()
        ))
    };
    // Files avoid pipe deadlock and reader threads retaining inherited pipe ends.
    // These are data only; the launcher never extracts executable files.
    let stdout = tempfile::tempfile().map_err(query_error)?;
    let stderr = tempfile::tempfile().map_err(query_error)?;
    let mut child = Command::new(qemu)
        .args(["-machine", "help"])
        .stdin(Stdio::null())
        .stdout(stdout.try_clone().map_err(query_error)?)
        .stderr(stderr.try_clone().map_err(query_error)?)
        .spawn()
        .map_err(query_error)?;
    let status = match child.wait_timeout(QUERY_TIMEOUT) {
        Ok(Some(status)) => status,
        result => {
            let primary = match result {
                Ok(None) => format!(
                    "QEMU machine query timed out after 5 seconds: {}",
                    qemu.display()
                ),
                Err(error) => format!("cannot wait for QEMU machine query: {error}"),
                Ok(Some(_)) => unreachable!(),
            };
            let kill_error = child.kill().err();
            let reap_error = child.wait().err();
            let mut message = primary;
            if let Some(error) = kill_error {
                message.push_str(&format!("\nQEMU query kill failed: {error}"));
            }
            if let Some(error) = reap_error {
                message.push_str(&format!("\nQEMU query reap failed: {error}"));
            }
            return Err(LaunchError::backend(message));
        }
    };
    let stdout = read_query_output(stdout).map_err(query_error)?;
    let stderr = read_query_output(stderr).map_err(query_error)?;
    if !status.success() {
        return Err(LaunchError::backend(format!(
            "QEMU machine query failed ({status}): {}\n{}",
            qemu.display(),
            stderr.trim()
        )));
    }
    if !stdout
        .lines()
        .any(|line| line.split_whitespace().next() == Some("dm-mc02"))
    {
        return Err(LaunchError::backend(format!(
            "{} does not provide the dm-mc02 machine; use the project QEMU with --qemu PATH.",
            qemu.display()
        )));
    }
    Ok(())
}

fn comma_escaped(value: &OsStr) -> OsString {
    // Preserve non-UTF-8 Unix paths while escaping QEMU's option delimiter.
    let bytes = value.as_encoded_bytes();
    let mut escaped = Vec::with_capacity(bytes.len());
    for &byte in bytes {
        escaped.push(byte);
        if byte == b',' {
            escaped.push(byte);
        }
    }
    // ASCII comma boundaries never split an encoded character.
    unsafe { OsString::from_encoded_bytes_unchecked(escaped) }
}

fn path_option(prefix: &str, path: &Path, suffix: &str) -> OsString {
    let mut value = OsString::from(prefix);
    value.push(comma_escaped(path.as_os_str()));
    value.push(suffix);
    value
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
    for slot in SERIAL_SLOTS {
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
