//! The sim/v1 schema. Admission describes compiled factories without creating
//! components or opening any native resource.

use super::{
    assertions::{data_hex, member, parse_assertion, required, string, unsigned},
    yaml::{
        fields, identifier, load_document, parse_time, text_bytes, ManifestError, Mapping, Value,
    },
};
use crate::{
    assertions::Assertion,
    can::{CanBusOptions, CanFrame},
    runtime::{
        self, CanDropRule, CanFaultProfile, CanNetworkOptions, HostInput, HostOptions,
        HostSimulation,
    },
};
use sim_core::{
    Capability, Direction, Endpoint, LinkSpec, NodeDescription, NodeSpec, RunMode,
    SimulationOptions, Topology,
};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};

type Result<T> = std::result::Result<T, ManifestError>;
fn error(value: impl std::fmt::Display) -> ManifestError {
    ManifestError::new(value.to_string())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Origin {
    Session,
    Ready,
    Marker(String),
}

#[derive(Clone, Debug)]
pub struct ExperimentInput {
    pub at_ns: u64,
    pub endpoint: Endpoint,
    pub payload: HostInput,
    pub origin: Origin,
}

#[derive(Clone, Debug)]
pub struct ReadyMarker {
    pub name: String,
    pub endpoint: Endpoint,
    pub text: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct ExperimentAssertion {
    pub assertion: Assertion,
    pub origin: Origin,
}

#[derive(Clone, Debug, Default)]
pub struct Experiment {
    pub ready: Vec<ReadyMarker>,
    pub inputs: Vec<ExperimentInput>,
    pub assertions: Vec<ExperimentAssertion>,
}

#[derive(Clone, Debug)]
pub struct Scene {
    pub source: PathBuf,
    pub topology: Topology<HostOptions>,
    pub mode: RunMode,
    pub quantum_ns: u64,
    pub duration_ns: u64,
    pub experiment: Option<Experiment>,
}

impl Scene {
    pub fn simulation(&self, mut options: SimulationOptions) -> sim_core::Result<HostSimulation> {
        options.mode = self.mode;
        options.quantum_ns = self.quantum_ns;
        HostSimulation::new(self.topology.clone(), runtime::registry()?, options)
    }
}

fn list<'a>(value: &'a Value, maximum: usize, label: &str) -> Result<&'a [Value]> {
    value
        .as_sequence()
        .filter(|items| items.len() <= maximum)
        .ok_or_else(|| {
            error(format!(
                "{label} requires a list of at most {maximum} entries"
            ))
        })
}

fn option_u64(
    mapping: &Mapping,
    key: &str,
    default: u64,
    maximum: u64,
    label: &str,
) -> Result<u64> {
    member(mapping, key)
        .map(|value| unsigned(value, maximum, &format!("{label}.{key}")))
        .unwrap_or(Ok(default))
}

#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
fn finite_number(value: &Value, label: &str) -> Result<f64> {
    let number = match value {
        Value::Float(number) => *number,
        Value::Integer(number) => number.to_string().parse::<f64>().map_err(error)?,
        _ => return Err(error(format!("{label} must be a finite plain number"))),
    };
    if number.is_finite() {
        Ok(number)
    } else {
        Err(error(format!("{label} must be finite")))
    }
}

#[cfg(any(feature = "devices", feature = "mujoco", feature = "gazebo"))]
fn native_name<'a>(value: &'a Value, label: &str) -> Result<&'a str> {
    let name = string(value, label)?;
    if name.is_empty() || name.len() > 4096 || name.contains('\0') {
        return Err(error(format!(
            "{label} must contain 1..4096 UTF-8 bytes without NUL"
        )));
    }
    Ok(name)
}

#[cfg(any(feature = "mujoco", feature = "gazebo"))]
fn world_node(options: &HostOptions) -> bool {
    match options {
        #[cfg(feature = "mujoco")]
        HostOptions::Mujoco(_) => true,
        #[cfg(feature = "gazebo")]
        HostOptions::Gazebo(_) => true,
        _ => false,
    }
}

fn parse_node(name: &str, value: &Value, _base_dir: &Path) -> Result<NodeSpec<HostOptions>> {
    identifier(&Value::String(name.to_owned()), "node name")?;
    let mapping = value
        .as_mapping()
        .ok_or_else(|| error(format!("node {name} requires a mapping")))?;
    let kind =
        member(mapping, "kind").ok_or_else(|| error(format!("node {name} requires a kind")))?;
    let kind = string(kind, &format!("node {name}.kind"))?;
    let options = match kind {
        #[cfg(all(target_os = "linux", feature = "socketcan"))]
        "host.socketcan" => {
            fields(
                value,
                &["kind", "interface", "enable_fd", "max_pending", "io_budget"],
                &["kind", "interface"],
                name,
            )?;
            HostOptions::SocketCan(crate::endpoints::socketcan::SocketCanOptions {
                interface: string(required(mapping, "interface"), "SocketCAN interface")?.into(),
                enable_fd: member(mapping, "enable_fd")
                    .map(|value| {
                        value
                            .as_bool()
                            .ok_or_else(|| error("SocketCAN enable_fd must be boolean"))
                    })
                    .unwrap_or(Ok(true))?,
                max_pending: option_u64(mapping, "max_pending", 256, 65536, name)? as usize,
                io_budget: option_u64(mapping, "io_budget", 64, 4096, name)? as usize,
            })
        }
        #[cfg(feature = "referee")]
        "device.referee" => {
            let options = Value::Mapping(
                mapping
                    .iter()
                    .filter(|(name, _)| name != "kind")
                    .cloned()
                    .collect(),
            );
            HostOptions::Referee(
                crate::devices::referee::config::parse_options(&options, name).map_err(error)?,
            )
        }
        #[cfg(feature = "mujoco")]
        "physics.mujoco" => {
            use crate::physics::{
                factory::{MujocoOptions, WorldControl},
                mujoco::MujocoBindings,
            };
            fields(
                value,
                &["kind", "model", "joints", "control", "imus"],
                &["kind", "model"],
                name,
            )?;
            let model = _base_dir.join(native_name(required(mapping, "model"), "MuJoCo model")?);
            let mut bindings = MujocoBindings::default();
            if let Some(value) = member(mapping, "joints") {
                let values = value
                    .as_mapping()
                    .ok_or_else(|| error("MuJoCo joints must be a joint-to-actuator mapping"))?;
                for (joint, actuator) in values {
                    native_name(&Value::String(joint.clone()), "MuJoCo joint")?;
                    bindings.joints.push((
                        joint.clone(),
                        native_name(actuator, "MuJoCo actuator")?.into(),
                    ));
                }
            }
            if let Some(value) = member(mapping, "imus") {
                let values = value
                    .as_mapping()
                    .ok_or_else(|| error("MuJoCo imus must be an alias-to-sensor-pair mapping"))?;
                for (alias, pair) in values {
                    identifier(&Value::String(alias.clone()), "IMU alias")?;
                    let pair = list(pair, 2, "IMU sensor pair")?;
                    if pair.len() != 2 {
                        return Err(error("IMU binding requires gyro and accelerometer names"));
                    }
                    bindings.imus.push((
                        alias.clone(),
                        native_name(&pair[0], "IMU gyro")?.into(),
                        native_name(&pair[1], "IMU accelerometer")?.into(),
                    ));
                }
            }
            let control = match member(mapping, "control")
                .map(|value| string(value, "MuJoCo control"))
                .transpose()?
                .unwrap_or("direct")
            {
                "direct" => WorldControl::Direct,
                "bound" => WorldControl::Bound,
                _ => return Err(error("MuJoCo control must be direct or bound")),
            };
            HostOptions::Mujoco(MujocoOptions {
                model,
                bindings,
                control,
            })
        }
        #[cfg(feature = "gazebo")]
        "physics.gazebo" => {
            use crate::physics::{
                factory::{GazeboOptions, WorldControl},
                gazebo::GazeboBindings,
            };
            fields(
                value,
                &[
                    "kind",
                    "model",
                    "joints",
                    "bodies",
                    "imus",
                    "control",
                    "resource_paths",
                ],
                &["kind", "model"],
                name,
            )?;
            let mut bindings = GazeboBindings::default();
            for key in ["joints", "bodies", "imus"] {
                let Some(value) = member(mapping, key) else {
                    continue;
                };
                let values = value.as_mapping().ok_or_else(|| {
                    error(format!(
                        "Gazebo {key} must be an alias-to-native-binding mapping"
                    ))
                })?;
                for (alias, native) in values {
                    native_name(&Value::String(alias.clone()), "Gazebo alias")?;
                    let count = if key == "imus" { 3 } else { 2 };
                    let native = list(native, count, "Gazebo native binding")?;
                    if native.len() != count {
                        return Err(error(format!(
                            "Gazebo {key} binding requires {count} names"
                        )));
                    }
                    let names = native
                        .iter()
                        .map(|value| native_name(value, "Gazebo native name").map(str::to_owned))
                        .collect::<Result<Vec<_>>>()?;
                    match key {
                        "joints" => bindings.joints.push((
                            alias.clone(),
                            names[0].clone(),
                            names[1].clone(),
                        )),
                        "bodies" => bindings.bodies.push((
                            alias.clone(),
                            names[0].clone(),
                            names[1].clone(),
                        )),
                        "imus" => bindings.imus.push((
                            alias.clone(),
                            names[0].clone(),
                            names[1].clone(),
                            names[2].clone(),
                        )),
                        _ => unreachable!(),
                    }
                }
            }
            let model = _base_dir.join(native_name(required(mapping, "model"), "Gazebo model")?);
            let resource_paths = member(mapping, "resource_paths")
                .map(|value| {
                    list(value, 64, "Gazebo resource paths")?
                        .iter()
                        .map(
                            |value| Ok(_base_dir.join(native_name(value, "Gazebo resource path")?)),
                        )
                        .collect::<Result<Vec<_>>>()
                })
                .transpose()?
                .unwrap_or_default();
            let control = match member(mapping, "control")
                .map(|value| string(value, "Gazebo control"))
                .transpose()?
                .unwrap_or("direct")
            {
                "direct" => WorldControl::Direct,
                "bound" => WorldControl::Bound,
                _ => return Err(error("Gazebo control must be direct or bound")),
            };
            HostOptions::Gazebo(GazeboOptions {
                model,
                bindings,
                resource_paths,
                control,
            })
        }
        #[cfg(feature = "devices")]
        "device.dji-m3508" => {
            use crate::{
                devices::dji::DeviceOptions,
                physics::factory::{M3508Options, MotorPlant},
            };
            let mut mandatory = vec!["kind", "motor_id", "plant"];
            match member(mapping, "plant").and_then(Value::as_str) {
                Some("joint") => mandatory.push("joint"),
                Some("fixed-rotor" | "rigid-rotor") => {
                    mandatory.extend(["position_rad", "velocity_rad_s"]);
                }
                _ => {}
            }
            fields(
                value,
                &[
                    "kind",
                    "motor_id",
                    "plant",
                    "position_rad",
                    "velocity_rad_s",
                    "joint",
                    "gear_ratio",
                    "torque_per_amp",
                    "command_timeout",
                    "feedback_period",
                    "temperature_c",
                ],
                &mandatory,
                name,
            )?;
            let number = |key: &str, default: f64| {
                member(mapping, key)
                    .map(|value| finite_number(value, key))
                    .unwrap_or(Ok(default))
            };
            let time = |key: &str, default: u64| {
                member(mapping, key)
                    .map(|value| parse_time(value, key, false))
                    .unwrap_or(Ok(default))
            };
            let device = DeviceOptions {
                motor_id: unsigned(required(mapping, "motor_id"), 8, "motor_id")? as u8,
                torque_per_amp: number("torque_per_amp", 0.02)?,
                command_timeout_ns: time("command_timeout", 100_000_000)?,
                feedback_period_ns: time("feedback_period", 1_000_000)?,
                temperature_c: option_u64(mapping, "temperature_c", 25, 255, "temperature_c")?
                    as u8,
            };
            let plant = match string(required(mapping, "plant"), "motor plant")? {
                "joint" => {
                    if member(mapping, "position_rad").is_some()
                        || member(mapping, "velocity_rad_s").is_some()
                    {
                        return Err(error("joint motor initial state belongs to its world"));
                    }
                    MotorPlant::Joint {
                        joint: native_name(required(mapping, "joint"), "joint name")?.into(),
                        gear_ratio: number(
                            "gear_ratio",
                            crate::devices::dji::joints::M3508_P19_RATIO,
                        )?,
                    }
                }
                kind @ ("fixed-rotor" | "rigid-rotor") => {
                    if member(mapping, "joint").is_some() || member(mapping, "gear_ratio").is_some()
                    {
                        return Err(error("joint and gear_ratio require plant joint"));
                    }
                    let position_rad =
                        finite_number(required(mapping, "position_rad"), "position_rad")?;
                    let velocity_rad_s =
                        finite_number(required(mapping, "velocity_rad_s"), "velocity_rad_s")?;
                    if kind == "fixed-rotor" {
                        MotorPlant::FixedRotor {
                            position_rad,
                            velocity_rad_s,
                        }
                    } else {
                        MotorPlant::RigidRotor(crate::physics::rotor::RotorOptions {
                            initial_position: position_rad,
                            initial_velocity: velocity_rad_s,
                            ..Default::default()
                        })
                    }
                }
                _ => {
                    return Err(error(
                        "motor plant must be fixed-rotor, rigid-rotor or joint",
                    ))
                }
            };
            HostOptions::M3508(M3508Options { device, plant })
        }
        #[cfg(all(target_os = "linux", feature = "host-processes"))]
        "app.host-processes" => {
            fields(value, &["kind", "commands"], &["kind", "commands"], name)?;
            let values = required(mapping, "commands")
                .as_mapping()
                .filter(|values| (1..=8).contains(&values.len()))
                .ok_or_else(|| error("commands must be a mapping with 1..8 entries"))?;
            let mut commands = Vec::new();
            for (name, value) in values {
                let name = identifier(&Value::String(name.clone()), "command name")?.to_owned();
                let item = fields(value, &["command"], &["command"], &name)?;
                let argv = list(required(item, "command"), 128, "command")?
                    .iter()
                    .map(|value| string(value, "command argument").map(str::to_owned))
                    .collect::<Result<_>>()?;
                commands.push(
                    crate::processes::HostCommand::admit(name, argv, _base_dir.to_owned())
                        .map_err(error)?,
                );
            }
            HostOptions::HostProcesses(crate::processes::HostProcessesOptions {
                commands,
                allow_launch: false,
            })
        }
        #[cfg(all(target_os = "linux", feature = "machine"))]
        "machine.qemu" => {
            fields(
                value,
                &["kind", "model", "firmware", "uarts", "remote", "adc"],
                &["kind", "firmware"],
                name,
            )?;
            if member(mapping, "model")
                .map(|v| string(v, "QEMU model"))
                .transpose()?
                .unwrap_or("dm-mc02")
                != "dm-mc02"
            {
                return Err(error("unsupported QEMU model; expected dm-mc02"));
            }
            let firmware = PathBuf::from(string(required(mapping, "firmware"), "QEMU firmware")?);
            let mut options = crate::machine::QemuOptions::new(if firmware.is_absolute() {
                firmware
            } else {
                _base_dir.join(firmware)
            });
            options.remote = member(mapping, "remote")
                .map(|value| {
                    value
                        .as_bool()
                        .ok_or_else(|| error("QEMU remote must be a boolean"))
                })
                .transpose()?
                .unwrap_or(false);
            options.adc = member(mapping, "adc")
                .map(|value| {
                    value
                        .as_bool()
                        .ok_or_else(|| error("QEMU adc must be a boolean"))
                })
                .transpose()?
                .unwrap_or(false);
            if let Some(uarts) = member(mapping, "uarts") {
                options.uarts = list(uarts, 5, "QEMU uarts")?
                    .iter()
                    .map(|v| string(v, "QEMU UART").map(str::to_owned))
                    .collect::<Result<_>>()?;
            }
            HostOptions::Qemu(options)
        }
        "device.can-host" => {
            fields(value, &["kind"], &["kind"], name)?;
            HostOptions::CanHost
        }
        "device.uart-echo" => {
            fields(value, &["kind"], &["kind"], name)?;
            HostOptions::UartEcho
        }
        "net.uart" => {
            fields(
                value,
                &[
                    "kind",
                    "max_pending",
                    "max_pending_bytes",
                    "dispatch_budget",
                    "delay_ns",
                ],
                &["kind"],
                name,
            )?;
            HostOptions::UartNet(crate::fabric::uart::UartNetOptions {
                max_pending: option_u64(mapping, "max_pending", 256, 65536, name)? as usize,
                max_pending_bytes: option_u64(
                    mapping,
                    "max_pending_bytes",
                    65536,
                    4096 * 65536,
                    name,
                )? as usize,
                dispatch_budget: option_u64(mapping, "dispatch_budget", 256, 65536, name)? as usize,
                delay_ns: option_u64(mapping, "delay_ns", 0, u64::MAX, name)?,
            })
        }
        "net.digital" => {
            fields(
                value,
                &["kind", "max_pending", "dispatch_budget"],
                &["kind"],
                name,
            )?;
            HostOptions::DigitalNet(crate::fabric::digital::DigitalNetOptions {
                max_pending: option_u64(mapping, "max_pending", 256, 65536, name)? as usize,
                dispatch_budget: option_u64(mapping, "dispatch_budget", 256, 65536, name)? as usize,
            })
        }
        "net.imu" => {
            fields(
                value,
                &["kind", "max_pending", "dispatch_budget"],
                &["kind"],
                name,
            )?;
            HostOptions::ImuNet(crate::fabric::imu::ImuNetOptions {
                max_pending: option_u64(mapping, "max_pending", 256, 65536, name)? as usize,
                dispatch_budget: option_u64(mapping, "dispatch_budget", 256, 65536, name)? as usize,
            })
        }
        "net.digital-edge" => {
            fields(
                value,
                &["kind", "max_pending", "dispatch_budget"],
                &["kind"],
                name,
            )?;
            HostOptions::DigitalEdgeNet(crate::fabric::edge::EdgeNetOptions {
                max_pending: option_u64(mapping, "max_pending", 256, 65536, name)? as usize,
                dispatch_budget: option_u64(mapping, "dispatch_budget", 256, 65536, name)? as usize,
            })
        }
        "bus.can" => {
            fields(
                value,
                &[
                    "kind",
                    "delay_ns",
                    "loopback",
                    "max_pending",
                    "dispatch_budget",
                    "fault_profiles",
                ],
                &["kind"],
                name,
            )?;
            let bus = CanBusOptions {
                delay_ns: option_u64(mapping, "delay_ns", 0, u64::MAX, name)?,
                loopback: member(mapping, "loopback")
                    .map(|value| {
                        value
                            .as_bool()
                            .ok_or_else(|| error(format!("{name}.loopback must be a boolean")))
                    })
                    .unwrap_or(Ok(false))?,
                max_pending: option_u64(mapping, "max_pending", 256, 65536, name)? as usize,
                dispatch_budget: option_u64(mapping, "dispatch_budget", 256, 65536, name)? as usize,
            };
            let mut fault_profiles = Vec::new();
            if let Some(profiles) = member(mapping, "fault_profiles") {
                let profiles = profiles
                    .as_mapping()
                    .ok_or_else(|| error("CAN fault_profiles requires a mapping"))?;
                for (name, profile) in profiles {
                    let profile = fields(profile, &["delay_ns", "drop"], &[], "CAN fault profile")?;
                    let delay_ns = member(profile, "delay_ns")
                        .map(|value| unsigned(value, u64::MAX, "CAN fault delay_ns"))
                        .transpose()?;
                    let mut drop = Vec::new();
                    if let Some(rules) = member(profile, "drop") {
                        for rule in list(rules, 64, "CAN drop rules")? {
                            let rule = fields(
                                rule,
                                &["source", "destination", "can_id"],
                                &[],
                                "CAN drop rule",
                            )?;
                            let endpoint = |key| {
                                member(rule, key)
                                    .map(|value| {
                                        Endpoint::parse(string(value, key)?).map_err(error)
                                    })
                                    .transpose()
                            };
                            drop.push(CanDropRule {
                                source: endpoint("source")?,
                                destination: endpoint("destination")?,
                                can_id: member(rule, "can_id")
                                    .map(|value| {
                                        unsigned(value, 0x1fff_ffff, "CAN drop can_id")
                                            .map(|v| v as u32)
                                    })
                                    .transpose()?,
                            });
                        }
                    }
                    fault_profiles.push(CanFaultProfile {
                        name: name.clone(),
                        delay_ns,
                        drop,
                    });
                }
            }
            HostOptions::CanBus(CanNetworkOptions {
                bus,
                fault_profiles,
            })
        }
        _ => {
            return Err(error(format!(
                "node {name}: unregistered component kind {kind:?}"
            )))
        }
    };
    Ok(NodeSpec::new(name, kind, options))
}

fn origin(value: Option<&Value>, markers: bool) -> Result<Origin> {
    match value
        .map(|v| string(v, "origin"))
        .transpose()?
        .unwrap_or("ready")
    {
        "session" => Ok(Origin::Session),
        "ready" => Ok(Origin::Ready),
        text if markers && text.starts_with("marker:") => {
            let name = &text[7..];
            identifier(&Value::String(name.to_owned()), "input marker name")?;
            Ok(Origin::Marker(name.to_owned()))
        }
        _ => Err(error(if markers {
            "input origin requires session, ready or marker:<name>"
        } else {
            "assertion origin requires session or ready"
        })),
    }
}

fn input_payload(scene: &Scene, endpoint: &Endpoint, value: &Value) -> Result<HostInput> {
    let node = scene
        .topology
        .nodes
        .iter()
        .find(|node| node.name == endpoint.node)
        .ok_or_else(|| error("unknown experiment input node"))?;
    match (&node.options, endpoint.port.as_str()) {
        #[cfg(feature = "referee")]
        (HostOptions::Referee(_), "enable") => {
            if value.as_bool() != Some(true) {
                return Err(error("referee enable requires true"));
            }
            Ok(HostInput::RefereeEnable(true))
        }
        #[cfg(any(feature = "mujoco", feature = "gazebo"))]
        (_, "effort") if world_node(&node.options) => {
            let mapping = value
                .as_mapping()
                .filter(|values| !values.is_empty())
                .ok_or_else(|| error("joint effort must be a nonempty joint-to-torque mapping"))?;
            Ok(HostInput::JointEffort(
                mapping
                    .iter()
                    .map(|(name, value)| Ok((name.clone(), finite_number(value, "joint effort")?)))
                    .collect::<Result<_>>()?,
            ))
        }
        (HostOptions::CanHost, "tx") => {
            let mapping = fields(
                value,
                &["id", "flags", "dlc", "data_hex"],
                &["id", "dlc", "data_hex"],
                "CAN input",
            )?;
            let frame = CanFrame::new(
                unsigned(required(mapping, "id"), 0x1fff_ffff, "CAN id")? as u32,
                option_u64(mapping, "flags", 0, 15, "CAN input")? as u32,
                Some(unsigned(required(mapping, "dlc"), 15, "CAN dlc")? as u8),
                data_hex(required(mapping, "data_hex"), "CAN data_hex")?,
                0,
            )
            .map_err(error)?;
            Ok(HostInput::CanFrame(frame))
        }
        (HostOptions::CanBus(_), "fault") => Ok(HostInput::FaultProfile(
            string(value, "CAN fault profile")?.to_owned(),
        )),
        #[cfg(all(target_os = "linux", feature = "machine"))]
        (HostOptions::Qemu(options), port) => {
            let input = if port.starts_with("adc1.") && options.adc {
                let mapping = fields(value, &["raw", "voltage_uv"], &[], "ADC input")?;
                if mapping.len() != 1 {
                    return Err(error("ADC input requires exactly raw or voltage_uv"));
                }
                if let Some(raw) = member(mapping, "raw") {
                    crate::profile::MachineInput::AdcRaw(
                        unsigned(raw, u16::MAX.into(), "ADC raw")? as u16
                    )
                } else {
                    crate::profile::MachineInput::AdcVoltage(unsigned(
                        required(mapping, "voltage_uv"),
                        u32::MAX.into(),
                        "ADC voltage_uv",
                    )? as u32)
                }
            } else if port == "key" {
                crate::profile::MachineInput::Bool(
                    value
                        .as_bool()
                        .ok_or_else(|| error("key input must be a boolean"))?,
                )
            } else if port.starts_with("gpio.") {
                crate::profile::MachineInput::Level(unsigned(value, 1, "GPIO level")? != 0)
            } else if port == "remote" && options.remote {
                let Value::Binary(bytes) = value else {
                    return Err(error("remote input must be raw bytes"));
                };
                crate::profile::MachineInput::Bytes(bytes.clone())
            } else if options.uarts.iter().any(|uart| uart == port) {
                let bytes = match value {
                    Value::Binary(bytes) if bytes.len() <= 4096 => bytes.clone(),
                    _ => text_bytes(value, "UART input", true, 4096)?,
                };
                crate::profile::MachineInput::Bytes(bytes)
            } else {
                return Err(error(format!(
                    "QEMU port cannot accept experiment inputs: {port}"
                )));
            };
            Ok(HostInput::Machine(input))
        }
        _ => Err(error(format!(
            "endpoint cannot accept experiment inputs: {endpoint}"
        ))),
    }
}

fn observation_endpoint(
    scene: &Scene,
    descriptions: &[NodeDescription],
    kind: &str,
    value: &str,
) -> Result<String> {
    let endpoint = Endpoint::parse(value).map_err(error)?;
    let index = scene
        .topology
        .nodes
        .iter()
        .position(|node| node.name == endpoint.node)
        .ok_or_else(|| error("unknown assertion node"))?;
    let description = &descriptions[index];
    if kind == "digital_equals" {
        let local = endpoint.port.strip_suffix(".out").unwrap_or(&endpoint.port);
        if (matches!(local, "key" | "led") || local.starts_with("gpio."))
            && description
                .ports
                .iter()
                .any(|port| port.protocol == "snapshot" && port.direction == Direction::Out)
        {
            if let Some(pin) = local.strip_prefix("gpio.") {
                crate::boards::gpio_pin(pin).map_err(error)?;
            }
            return Ok(endpoint.to_string());
        }
    }
    let port = description.port(&endpoint.port).map_err(error)?;
    let protocol = match kind {
        "uart_contains" => "uart",
        "can_seen" => "can",
        _ => "digital",
    };
    if port.protocol != protocol
        || port.direction == Direction::In
        || matches!(protocol, "uart" | "can") && description.has(Capability::Connection)
    {
        return Err(error(format!(
            "endpoint has no individual public {protocol} observations: {endpoint}"
        )));
    }
    Ok(endpoint.to_string())
}

pub fn load_scene(path: impl AsRef<Path>) -> Result<Scene> {
    let (source, value) = load_document(path)?;
    parse_scene(source, &value)
}

/// In-memory counterpart used by callers with an already admitted document.
pub fn parse_scene(source: PathBuf, value: &Value) -> Result<Scene> {
    let document = fields(
        value,
        &["schema", "runtime", "nodes", "links", "experiment"],
        &["schema", "nodes"],
        "scene",
    )?;
    if string(required(document, "schema"), "scene.schema")? != "sim/v1" {
        return Err(error("scene.schema must be sim/v1"));
    }
    let empty = Value::Mapping(vec![]);
    let runtime = fields(
        member(document, "runtime").unwrap_or(&empty),
        &["mode", "quantum", "duration"],
        &[],
        "runtime",
    )?;
    let mode = match member(runtime, "mode")
        .map(|v| string(v, "runtime.mode"))
        .transpose()?
        .unwrap_or("stepped")
    {
        "stepped" => RunMode::Stepped,
        "interactive" => RunMode::Interactive,
        _ => return Err(error("runtime.mode must be stepped or interactive")),
    };
    let time = |key, default: &str| {
        parse_time(
            member(runtime, key).unwrap_or(&Value::String(default.into())),
            &format!("runtime.{key}"),
            false,
        )
    };
    let quantum_ns = time("quantum", "1ms")?;
    let duration_ns = time("duration", "200ms")?;
    let nodes = required(document, "nodes")
        .as_mapping()
        .filter(|nodes| (1..=64).contains(&nodes.len()))
        .ok_or_else(|| error("nodes must contain 1..64 named components"))?;
    let nodes = nodes
        .iter()
        .map(|(name, value)| parse_node(name, value, source.parent().unwrap_or(Path::new("."))))
        .collect::<Result<Vec<_>>>()?;
    let mut links = Vec::new();
    if let Some(values) = member(document, "links") {
        for pair in list(values, 256, "links")? {
            let pair = list(pair, 2, "link pair")?;
            if pair.len() != 2 {
                return Err(error("each link requires two qualified endpoints"));
            }
            let endpoint = |value| -> Result<Endpoint> {
                let text = text_bytes(value, "link endpoint", false, 256)?;
                Endpoint::parse(std::str::from_utf8(&text).map_err(error)?).map_err(error)
            };
            links.push(LinkSpec::new(endpoint(&pair[0])?, endpoint(&pair[1])?));
        }
    }
    let mut scene = Scene {
        source,
        topology: Topology::new(nodes, links).map_err(error)?,
        mode,
        quantum_ns,
        duration_ns,
        experiment: None,
    };
    let simulation = scene
        .simulation(SimulationOptions::default())
        .map_err(error)?;
    let descriptions = simulation.validate();
    let Some(experiment) = member(document, "experiment") else {
        return Ok(scene);
    };
    if scene.topology.nodes.len() > 63 || mode != RunMode::Stepped {
        return Err(error(
            "finite experiment requires stepped mode and at most 63 nodes",
        ));
    }
    let mapping = fields(
        experiment,
        &["ready", "inputs", "assertions"],
        &[],
        "experiment",
    )?;
    let mut experiment = Experiment::default();
    let mut markers = BTreeSet::new();
    if let Some(values) = member(mapping, "ready") {
        for value in list(values, 64, "experiment.ready")? {
            let mapping = fields(
                value,
                &["name", "endpoint", "text"],
                &["name", "endpoint", "text"],
                "ready marker",
            )?;
            let name = string(required(mapping, "name"), "ready name")?;
            identifier(&Value::String(name.into()), "ready name")?;
            if !markers.insert(name.to_owned()) {
                return Err(error("ready marker names must be unique"));
            }
            let endpoint = observation_endpoint(
                &scene,
                descriptions,
                "uart_contains",
                string(required(mapping, "endpoint"), "ready endpoint")?,
            )?;
            experiment.ready.push(ReadyMarker {
                name: name.into(),
                endpoint: Endpoint::parse(&endpoint).map_err(error)?,
                text: text_bytes(required(mapping, "text"), "ready text", false, 4096)?,
            });
        }
    }
    if let Some(values) = member(mapping, "inputs") {
        for value in list(values, 1024, "experiment.inputs")? {
            let mapping = fields(
                value,
                &["at", "endpoint", "value", "origin"],
                &["at", "endpoint", "value"],
                "experiment input",
            )?;
            let at_ns = parse_time(required(mapping, "at"), "input.at", true)?;
            if at_ns > duration_ns {
                return Err(error("input exceeds scene duration"));
            }
            let origin = origin(member(mapping, "origin"), true)?;
            if let Origin::Marker(name) = &origin {
                if !markers.contains(name) {
                    return Err(error("input names an unknown ready marker"));
                }
            }
            let endpoint =
                Endpoint::parse(string(required(mapping, "endpoint"), "input.endpoint")?)
                    .map_err(error)?;
            let payload = simulation
                .validate_input(
                    &endpoint,
                    input_payload(&scene, &endpoint, required(mapping, "value"))?,
                )
                .map_err(error)?;
            experiment.inputs.push(ExperimentInput {
                at_ns,
                endpoint,
                payload,
                origin,
            });
        }
    }
    if let Some(values) = member(mapping, "assertions") {
        for (index, value) in list(values, 1024, "experiment.assertions")?
            .iter()
            .enumerate()
        {
            let mapping = value
                .as_mapping()
                .ok_or_else(|| error("experiment assertion requires a mapping"))?;
            let origin = origin(member(mapping, "origin"), false)?;
            let condition = Value::Mapping(
                mapping
                    .iter()
                    .filter(|(key, _)| key != "origin")
                    .cloned()
                    .collect(),
            );
            let assertion = parse_assertion(&condition, index, |kind, endpoint| {
                observation_endpoint(&scene, descriptions, kind, endpoint)
            })?;
            if assertion.before_ns > duration_ns {
                return Err(error("assertion deadline exceeds scene duration"));
            }
            experiment
                .assertions
                .push(ExperimentAssertion { assertion, origin });
        }
    }
    scene.experiment = Some(experiment);
    Ok(scene)
}
