//! Shared native launch admission. Process execution remains with its consumer.
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;
use wait_timeout::ChildExt;
const QUERY_TIMEOUT: Duration = Duration::from_secs(5);
const QUERY_OUTPUT_LIMIT: u64 = 128 * 1024;
#[derive(Debug)]
pub struct LaunchError {
    pub code: i32,
    pub message: String,
}
impl std::fmt::Display for LaunchError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}
impl std::error::Error for LaunchError {}

impl LaunchError {
    pub fn admission(message: impl Into<String>) -> Self {
        Self {
            code: 1,
            message: message.into(),
        }
    }

    pub fn backend(message: impl Into<String>) -> Self {
        Self {
            code: 78,
            message: message.into(),
        }
    }
}

pub fn readable_file(path: &Path, label: &str) -> Result<PathBuf, LaunchError> {
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

pub fn socket_path(path: Option<&Path>) -> Result<Option<PathBuf>, LaunchError> {
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

pub fn resolve_qemu(explicit: Option<PathBuf>) -> Result<PathBuf, LaunchError> {
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

pub fn check_machine(qemu: &Path) -> Result<(), LaunchError> {
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

pub fn path_option(prefix: &str, path: &Path, suffix: &str) -> OsString {
    let mut value = OsString::from(prefix);
    value.push(comma_escaped(path.as_os_str()));
    value.push(suffix);
    value
}

/// Compose native debug and chardev arguments. The consumer chooses execution
/// mode, owns the process/deadlines, and supplies an admitted machine profile.
pub fn native_qemu_command(
    qemu: &Path,
    machine_arguments: impl IntoIterator<Item = OsString>,
    serial_slots: &[&str],
    streams: &[String],
    qmp_path: Option<&Path>,
    gdb_port: Option<u16>,
) -> Result<Vec<OsString>, LaunchError> {
    let mut argv = vec![qemu.as_os_str().to_owned()];
    argv.extend(machine_arguments);
    argv.extend(
        [
            "-nodefaults",
            "-display",
            "none",
            "-monitor",
            "none",
            "-S",
            "-qmp",
            "unix:{qmp},server=on,wait=off",
        ]
        .map(OsString::from),
    );
    for slot in serial_slots {
        if streams.iter().any(|stream| stream == slot) {
            argv.extend([
                "-chardev".into(),
                format!("socket,id={slot},path={{{slot}}},server=on,wait=off").into(),
                "-serial".into(),
                format!("chardev:{slot}").into(),
            ]);
        } else {
            argv.extend(["-serial".into(), "none".into()]);
        }
    }
    if let Some(path) = socket_path(qmp_path)? {
        argv.extend([
            "-qmp".into(),
            path_option("unix:", &path, ",server=on,wait=off"),
        ]);
    }
    if let Some(port) = gdb_port {
        if port == 0 {
            return Err(LaunchError::admission("GDB port requires 1..65535"));
        }
        argv.extend(["-gdb".into(), format!("tcp:127.0.0.1:{port}").into()]);
    }
    Ok(argv)
}
