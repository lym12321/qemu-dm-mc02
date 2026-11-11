//! Portable Lab configuration. Loading and validation never start a process.
use super::{
    BoardCommand, ImuMode, LabInput, LabOptions, StartupMode, channels::AdcValue,
    command::decode_uart,
};
use crate::{boards, can};
use serde::{Deserialize, Serialize};
use sim_core::{Error, Result};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{Read, Write},
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{Arc, atomic::AtomicBool},
    time::{Duration, Instant},
};

pub const SCHEMA: &str = "sim/lab-project/v1";
const FILE_LIMIT: u64 = 256 * 1024;
fn refused(message: impl Into<String>) -> sim_core::Failure {
    Arc::new(Error::Admission(message.into()))
}
fn all_uarts() -> Vec<String> {
    boards::launch::UARTS
        .iter()
        .map(|port| (*port).into())
        .collect()
}
fn all_cans() -> Vec<String> {
    boards::launch::CANS
        .iter()
        .map(|port| (*port).into())
        .collect()
}
fn enabled() -> bool {
    true
}
fn stationary_imu() -> ImuMode {
    ImuMode::Stationary
}
fn hold_ms() -> u64 {
    1000
}
fn startup_secs() -> f64 {
    10.0
}
fn timeout_secs() -> f64 {
    3600.0
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LabProject {
    pub schema: String,
    #[serde(default)]
    pub firmware: Option<PathBuf>,
    #[serde(default)]
    pub watch_elf: bool,
    #[serde(default = "all_uarts")]
    pub uarts: Vec<String>,
    #[serde(default = "all_cans")]
    pub cans: Vec<String>,
    #[serde(default = "enabled")]
    pub adc: bool,
    #[serde(default = "stationary_imu")]
    pub imu: ImuMode,
    #[serde(default)]
    pub startup: StartupMode,
    #[serde(default = "hold_ms")]
    pub user_hold_ms: u64,
    #[serde(default = "startup_secs")]
    pub startup_timeout_secs: f64,
    #[serde(default = "timeout_secs")]
    pub timeout_secs: f64,
    #[serde(default)]
    pub gdb_port: Option<u16>,
    #[serde(default)]
    pub usbip: Option<SocketAddr>,
    #[serde(default, deserialize_with = "shortcut_map")]
    pub actions: BTreeMap<String, Shortcut>,
}
impl Default for LabProject {
    fn default() -> Self {
        Self {
            schema: SCHEMA.into(),
            firmware: None,
            watch_elf: false,
            uarts: all_uarts(),
            cans: all_cans(),
            adc: true,
            imu: ImuMode::Stationary,
            startup: StartupMode::Run,
            user_hold_ms: hold_ms(),
            startup_timeout_secs: startup_secs(),
            timeout_secs: timeout_secs(),
            gdb_port: None,
            usbip: None,
            actions: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Shortcut {
    Key {
        pressed: bool,
    },
    Gpio {
        pin: String,
        level: bool,
    },
    Adc {
        channel: u16,
        kind: AdcKind,
        value: u32,
    },
    Uart {
        endpoint: String,
        format: String,
        ending: String,
        data: String,
    },
    Can {
        endpoint: String,
        id: u32,
        #[serde(default)]
        extended: bool,
        #[serde(default)]
        rtr: bool,
        #[serde(default)]
        fd: bool,
        #[serde(default)]
        brs: bool,
        #[serde(default)]
        dlc: Option<u8>,
        data: String,
    },
    Vin {
        millivolts: u32,
    },
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AdcKind {
    Raw,
    VoltageUv,
}

fn shortcut_map<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<BTreeMap<String, Shortcut>, D::Error> {
    struct Actions;
    impl<'de> serde::de::Visitor<'de> for Actions {
        type Value = BTreeMap<String, Shortcut>;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("distinct named shortcut actions")
        }
        fn visit_map<M: serde::de::MapAccess<'de>>(
            self,
            mut source: M,
        ) -> std::result::Result<Self::Value, M::Error> {
            let mut actions = BTreeMap::new();
            while let Some((name, action)) = source.next_entry::<String, Shortcut>()? {
                if actions.insert(name, action).is_some() {
                    return Err(serde::de::Error::custom("duplicate action name"));
                }
                if actions.len() > 32 {
                    return Err(serde::de::Error::custom("at most 32 shortcut actions"));
                }
            }
            Ok(actions)
        }
    }
    deserializer.deserialize_map(Actions)
}

impl Shortcut {
    pub fn to_command(&self) -> Result<BoardCommand> {
        Ok(match self {
            Self::Key { pressed } => BoardCommand::Input(LabInput::Key(*pressed)),
            Self::Gpio { pin, level } => {
                boards::gpio_pin(pin).map_err(|error| refused(error.to_string()))?;
                BoardCommand::Input(LabInput::Gpio {
                    pin: pin.clone(),
                    level: *level,
                })
            }
            Self::Adc {
                channel,
                kind,
                value,
            } => {
                if *channel > 31 {
                    return Err(refused("ADC1 channel must be in 0..31"));
                }
                let value = match kind {
                    AdcKind::Raw => AdcValue::Raw(
                        u16::try_from(*value).map_err(|_| refused("ADC raw requires 0..65535"))?,
                    ),
                    AdcKind::VoltageUv if *value <= 3_300_000 => AdcValue::Voltage(*value),
                    AdcKind::VoltageUv => {
                        return Err(refused("ADC voltage_uv requires 0..3300000"));
                    }
                };
                BoardCommand::Input(LabInput::Adc {
                    channel: *channel,
                    value,
                })
            }
            Self::Uart {
                endpoint,
                format,
                ending,
                data,
            } => {
                if !boards::launch::UARTS.contains(&endpoint.as_str()) {
                    return Err(refused("unsupported UART endpoint"));
                }
                BoardCommand::Input(LabInput::Uart {
                    endpoint: endpoint.clone(),
                    data: decode_uart(format, ending, data).map_err(refused)?,
                })
            }
            Self::Can {
                endpoint,
                id,
                extended,
                rtr,
                fd,
                brs,
                dlc,
                data,
            } => {
                if !boards::launch::CANS.contains(&endpoint.as_str()) {
                    return Err(refused("unsupported CAN endpoint"));
                }
                let flags = (u32::from(*extended) * can::EXTENDED)
                    | (u32::from(*rtr) * can::RTR)
                    | (u32::from(*fd) * can::FD)
                    | (u32::from(*brs) * can::BRS);
                let frame = can::CanFrame::new(
                    *id,
                    flags,
                    *dlc,
                    decode_uart("hex", "none", data).map_err(refused)?,
                    0,
                )
                .map_err(|error| refused(error.to_string()))?;
                BoardCommand::Input(LabInput::Can {
                    endpoint: endpoint.clone(),
                    frame,
                })
            }
            Self::Vin { millivolts } => BoardCommand::Vin(*millivolts),
        })
    }
}

fn duration(seconds: f64) -> Result<Duration> {
    Duration::try_from_secs_f64(seconds)
        .ok()
        .filter(|duration| !duration.is_zero())
        .ok_or_else(|| refused("timeouts must be finite and positive"))
}
impl LabProject {
    pub fn validate(&self) -> Result<()> {
        if self.schema != SCHEMA {
            return Err(refused(format!("project schema must be {SCHEMA}")));
        }
        if self.watch_elf && self.firmware.is_none() {
            return Err(refused("ELF watching requires a local firmware path"));
        }
        if let Some(path) = &self.firmware {
            if path.as_os_str().is_empty()
                || path
                    .as_os_str()
                    .as_encoded_bytes()
                    .iter()
                    .any(|byte| matches!(byte, 0 | b'\r' | b'\n'))
            {
                return Err(refused(
                    "firmware path must be nonempty and contain no NUL or newline",
                ));
            }
        }
        let startup = duration(self.startup_timeout_secs)?;
        let total = duration(self.timeout_secs)?;
        if startup > total || Instant::now().checked_add(total).is_none() {
            return Err(refused(
                "startup timeout must not exceed total host timeout",
            ));
        }
        if self.user_hold_ms == 0 || self.user_hold_ms.checked_mul(1_000_000).is_none() {
            return Err(refused(
                "USER hold requires a positive representable virtual duration",
            ));
        }
        if self.gdb_port == Some(0)
            || self.startup == StartupMode::WaitGdb && self.gdb_port.is_none()
        {
            return Err(refused("Wait GDB requires an enabled GDB port in 1..65535"));
        }
        if self
            .usbip
            .is_some_and(|address| !address.ip().is_loopback() || address.port() == 0)
        {
            return Err(refused(
                "USB/IP requires a loopback address and port in 1..65535",
            ));
        }
        for (ports, allowed) in [
            (&self.uarts, boards::launch::UARTS),
            (&self.cans, boards::launch::CANS),
        ] {
            let mut seen = BTreeSet::new();
            if ports
                .iter()
                .any(|port| !allowed.contains(&port.as_str()) || !seen.insert(port))
            {
                return Err(refused(
                    "project channels must be distinct supported board ports",
                ));
            }
        }
        if self.actions.len() > 32 {
            return Err(refused("project supports at most 32 shortcut actions"));
        }
        for (name, shortcut) in &self.actions {
            if name.is_empty() || name.len() > 64 || name.chars().any(char::is_control) {
                return Err(refused(
                    "action names require 1..64 bytes without control characters",
                ));
            }
            let command = shortcut.to_command()?;
            let enabled = match &command {
                BoardCommand::Input(LabInput::Uart { endpoint, .. }) => {
                    self.uarts.contains(endpoint)
                }
                BoardCommand::Input(LabInput::Can { endpoint, .. }) => self.cans.contains(endpoint),
                BoardCommand::Input(LabInput::Adc { .. }) => self.adc,
                _ => true,
            };
            if !enabled {
                return Err(refused(format!("action {name:?} uses a disabled channel")));
            }
        }
        Ok(())
    }
    pub fn commands(&self) -> Result<BTreeMap<String, BoardCommand>> {
        self.validate()?;
        self.actions
            .iter()
            .map(|(name, action)| Ok((name.clone(), action.to_command()?)))
            .collect()
    }
    pub fn lab_options(
        &self,
        qemu: Option<PathBuf>,
        qmp_path: Option<PathBuf>,
        cancellation: Option<Arc<AtomicBool>>,
    ) -> Result<LabOptions> {
        self.validate()?;
        let firmware = self
            .firmware
            .clone()
            .ok_or_else(|| refused("select a firmware ELF"))?;
        Ok(LabOptions {
            firmware,
            qemu,
            qmp_path,
            cancellation,
            uarts: self.uarts.clone(),
            cans: self.cans.clone(),
            adc: self.adc,
            imu: self.imu,
            usbip: self.usbip,
            gdb_port: self.gdb_port,
            timeout: duration(self.timeout_secs)?,
            startup_timeout: duration(self.startup_timeout_secs)?,
        })
    }
    /// Native attach configurations; the IDE keeps the local ELF and ARM GDB.
    pub fn ide_config(&self) -> Result<serde_json::Value> {
        self.validate()?;
        let firmware = self
            .firmware
            .as_ref()
            .ok_or_else(|| refused("select a firmware ELF"))?;
        let firmware = firmware
            .to_str()
            .ok_or_else(|| refused("IDE configuration requires a UTF-8 firmware path"))?;
        let port = self
            .gdb_port
            .ok_or_else(|| refused("enable GDB before exporting an IDE configuration"))?;
        let target = format!("127.0.0.1:{port}");
        let escaped = firmware.replace('\\', "\\\\").replace('"', "\\\"");
        Ok(serde_json::json!({
            "vscode": { "version":"0.2.0", "configurations":[{
                "name":"DM-MC02", "type":"cortex-debug", "request":"attach",
                "servertype":"external", "gdbTarget":target, "executable":firmware,
                "gdbPath":"arm-none-eabi-gdb"
            }]},
            "clion": { "configuration":"Remote Debug", "debugger":"arm-none-eabi-gdb", "target":target, "symbol_file":firmware },
            "gdb":format!("file \"{escaped}\"\ntarget remote {target}\n")
        }))
    }
}

/// The path is pinned at open; saving cannot select another destination.
#[derive(Debug)]
pub struct ProjectFile {
    path: PathBuf,
    project: LabProject,
}
impl ProjectFile {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let name = path
            .file_name()
            .ok_or_else(|| refused("project requires a file path"))?;
        let parent = path
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let parent = dunce::canonicalize(parent)
            .map_err(|error| refused(format!("project directory: {error}")))?;
        let path = parent.join(name);
        let project = match File::open(&path) {
            Ok(file) => {
                let mut bytes = Vec::new();
                file.take(FILE_LIMIT + 1)
                    .read_to_end(&mut bytes)
                    .map_err(|error| refused(format!("read project: {error}")))?;
                if bytes.len() as u64 > FILE_LIMIT {
                    return Err(refused("project exceeds 256 KiB"));
                }
                serde_json::from_slice::<LabProject>(&bytes)
                    .map_err(|error| refused(format!("invalid project: {error}")))?
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => LabProject::default(),
            Err(error) => return Err(refused(format!("read project: {error}"))),
        };
        project.validate()?;
        Ok(Self { path, project })
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn project(&self) -> &LabProject {
        &self.project
    }
    pub fn resolve(&self, project: &LabProject) -> Result<LabProject> {
        project.validate()?;
        let mut resolved = project.clone();
        resolved.firmware = project.firmware.as_ref().map(|path| {
            if path.is_absolute() {
                path.clone()
            } else {
                self.path.parent().expect("pinned parent").join(path)
            }
        });
        Ok(resolved)
    }
    pub fn save(&self, project: &LabProject) -> Result<()> {
        project.validate()?;
        let mut bytes = serde_json::to_vec_pretty(project)
            .map_err(|error| refused(format!("encode project: {error}")))?;
        bytes.push(b'\n');
        if bytes.len() as u64 > FILE_LIMIT {
            return Err(refused("project exceeds 256 KiB"));
        }
        let mut temporary =
            tempfile::NamedTempFile::new_in(self.path.parent().expect("pinned parent"))
                .map_err(|error| refused(format!("save project: {error}")))?;
        temporary
            .write_all(&bytes)
            .and_then(|_| temporary.as_file().sync_all())
            .map_err(|error| refused(format!("save project: {error}")))?;
        temporary
            .persist(&self.path)
            .map_err(|error| refused(format!("replace project: {error}")))?;
        Ok(())
    }
}
