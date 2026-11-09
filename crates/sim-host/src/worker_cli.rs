use crate::cli_support::{error_code, seconds, Signals};
use clap::Args;
use sim_host::{
    launch::LaunchError,
    worker::{
        engine::EngineOptions,
        motor_protocol::{DmOptions, MotorProtocol},
        Protocol, WorkerOptions,
    },
};
use std::{path::PathBuf, time::Duration};

#[derive(Args)]
pub struct Options {
    #[arg(long)]
    cosim: PathBuf,
    #[arg(long, default_value = "v1", value_parser = ["v1", "v2"])]
    protocol: String,
    #[arg(long)]
    wait_step_done: bool,
    #[arg(long)]
    fdcan: Option<PathBuf>,
    #[arg(long)]
    socketcan: Option<String>,
    /// Compose C620 motors on a CAN bus instead of the legacy motor backend.
    #[cfg(feature = "devices")]
    #[arg(long, value_parser = ["rotor", "four-wheel"])]
    can_profile: Option<String>,
    #[cfg(feature = "devices")]
    #[arg(long, requires = "can_profile", value_name = "ID")]
    dji_id: Vec<u8>,
    #[cfg(feature = "devices")]
    #[arg(long, default_value_t = 0)]
    can_delay_ns: u64,
    #[cfg(feature = "devices")]
    #[arg(long, default_value_t = 100_000_000)]
    command_timeout_ns: u64,
    #[cfg(feature = "devices")]
    #[arg(long, default_value_t = 0.02)]
    torque_per_amp: f64,
    /// Compiled backend: null, mujoco or ros2 (requires the matching build feature).
    #[arg(long, default_value = "null")]
    engine: String,
    #[arg(long)]
    mujoco_model: Option<PathBuf>,
    #[arg(long, default_value = "auto", value_parser = ["auto", "float", "dm-mit"])]
    motor_protocol: String,
    #[arg(long, default_value = "1", value_parser = integer)]
    dm_slave_id: u32,
    #[arg(long, default_value = "0x11", value_parser = integer)]
    dm_feedback_id: u32,
    #[arg(long, value_name = "INDEX:SLAVE_ID:FEEDBACK_ID", value_parser = motor_map)]
    dm_motor: Vec<(usize, u32, u32)>,
    #[arg(long, default_value_t = 12.5)]
    dm_p_max: f64,
    #[arg(long, default_value_t = 30.0)]
    dm_v_max: f64,
    #[arg(long, default_value_t = 10.0)]
    dm_t_max: f64,
    #[arg(long, default_value_t = 4)]
    motor_count: usize,
    #[arg(long, default_value = "0x200", value_parser = integer)]
    motor_can_base: u32,
    #[arg(long, default_value_t = 1000.0)]
    rate: f64,
    #[arg(long, value_name = "CHANNEL:RAW", value_parser = adc_raw)]
    adc_input: Vec<(u16, u16)>,
    #[arg(long, value_name = "CHANNEL:VOLTS", value_parser = adc_voltage)]
    adc_voltage: Vec<(u16, u32)>,
    #[arg(long)]
    realtime: bool,
    #[arg(long, default_value_t = 0)]
    frames: u64,
    #[arg(long, default_value = "10", value_parser = seconds)]
    connect_timeout: Duration,
    /// Optional total active host budget; cleanup has separate limits.
    #[arg(long, value_parser = seconds)]
    timeout: Option<Duration>,
    #[arg(long)]
    verbose: bool,
    #[cfg(feature = "ros2")]
    #[command(flatten)]
    ros: RosOptions,
}

#[cfg(feature = "ros2")]
#[derive(Args)]
struct RosOptions {
    #[arg(long, default_value = "/dm_mc02/motor_cmd")]
    ros_motor_topic: String,
    #[arg(long, default_value = "/dm_mc02/imu")]
    ros_imu_topic: String,
    #[arg(long, default_value = "/dm_mc02/joint_states")]
    ros_joint_state_topic: String,
    #[arg(long, default_value = "dm_mc02_sim_worker")]
    ros_node_name: String,
    #[arg(long)]
    ros_wait_imu: bool,
    #[arg(long, default_value = "")]
    ros_reset_service: String,
    #[arg(long, default_value = "2", value_parser = seconds)]
    ros_reset_timeout: Duration,
}

fn integer(text: &str) -> Result<u32, String> {
    let text = text.strip_prefix('+').unwrap_or(text);
    for (prefix, radix) in [
        ("0x", 16),
        ("0X", 16),
        ("0o", 8),
        ("0O", 8),
        ("0b", 2),
        ("0B", 2),
    ] {
        if let Some(digits) = text.strip_prefix(prefix) {
            return u32::from_str_radix(digits, radix)
                .map_err(|_| "requires an unsigned integer".into());
        }
    }
    text.parse()
        .map_err(|_| "requires an unsigned integer".into())
}
fn motor_map(text: &str) -> Result<(usize, u32, u32), String> {
    let fields: Vec<_> = text.split(':').collect();
    let [index, slave, feedback] = fields.as_slice() else {
        return Err("requires INDEX:SLAVE_ID:FEEDBACK_ID".into());
    };
    Ok((
        integer(index)? as usize,
        integer(slave)?,
        integer(feedback)?,
    ))
}
fn adc_raw(text: &str) -> Result<(u16, u16), String> {
    let (channel, raw) = text.split_once(':').ok_or("requires CHANNEL:RAW")?;
    let channel = integer(channel)?;
    let raw = integer(raw)?;
    if channel > 31 || raw > 65535 {
        return Err("ADC channel must be 0..31 and raw 0..65535".into());
    }
    Ok((channel as u16, raw as u16))
}
fn adc_voltage(text: &str) -> Result<(u16, u32), String> {
    let (channel, volts) = text.split_once(':').ok_or("requires CHANNEL:VOLTS")?;
    let channel = integer(channel)?;
    let voltage = (volts.parse::<f64>().map_err(|_| "voltage must be finite")? * 1_000_000.0)
        .round_ties_even();
    if channel > 31 || !voltage.is_finite() || !(0.0..=3_300_000.0).contains(&voltage) {
        return Err("ADC channel must be 0..31 and voltage 0..3.3 V".into());
    }
    Ok((channel as u16, voltage as u32))
}
fn admission(message: impl Into<String>) -> LaunchError {
    LaunchError {
        code: 2,
        message: message.into(),
    }
}

pub fn run(args: Options) -> Result<i32, LaunchError> {
    let engine = match args.engine.as_str() {
        "null" => EngineOptions::Null,
        #[cfg(feature = "mujoco")]
        "mujoco" => EngineOptions::Mujoco {
            model: args
                .mujoco_model
                .clone()
                .ok_or_else(|| admission("--mujoco-model is required with --engine mujoco"))?,
        },
        #[cfg(feature = "ros2")]
        "ros2" => EngineOptions::Ros2 {
            options: sim_host::ros2::Ros2Options {
                motor_count: args.motor_count,
                motor_topic: args.ros.ros_motor_topic,
                imu_topic: args.ros.ros_imu_topic,
                joint_state_topic: args.ros.ros_joint_state_topic,
                node_name: args.ros.ros_node_name,
                reset_service: args.ros.ros_reset_service,
            },
            wait_for_imu: args.ros.ros_wait_imu,
            reset_timeout: args.ros.ros_reset_timeout,
        },
        other => {
            return Err(admission(format!(
                "worker backend {other:?} is not compiled in"
            )))
        }
    };
    let signals = Signals::new().map_err(|error| LaunchError {
        code: 1,
        message: error.to_string(),
    })?;
    let mut options = WorkerOptions::new(args.cosim);
    options.protocol = if args.protocol == "v2" {
        Protocol::V2
    } else {
        Protocol::V1
    };
    options.wait_step_done = args.wait_step_done;
    options.fdcan = args.fdcan;
    options.socketcan = args.socketcan;
    #[cfg(feature = "devices")]
    {
        use sim_host::worker::can_graph::{CanProfile, RotorProfile};
        options.can_profile = match args.can_profile.as_deref() {
            None => None,
            Some("rotor") => Some(CanProfile::Rotor(RotorProfile {
                motor_ids: if args.dji_id.is_empty() {
                    vec![1]
                } else {
                    args.dji_id
                },
                delay_ns: args.can_delay_ns,
                command_timeout_ns: args.command_timeout_ns,
                torque_per_amp: args.torque_per_amp,
            })),
            #[cfg(feature = "mujoco")]
            Some("four-wheel") => {
                if !args.dji_id.is_empty() {
                    return Err(admission("four-wheel fixes motor IDs 1..4; omit --dji-id"));
                }
                Some(CanProfile::FourWheel {
                    model: args.mujoco_model.ok_or_else(|| {
                        admission("--mujoco-model is required with --can-profile four-wheel")
                    })?,
                    delay_ns: args.can_delay_ns,
                    command_timeout_ns: args.command_timeout_ns,
                    torque_per_amp: args.torque_per_amp,
                })
            }
            Some(profile) => {
                return Err(admission(format!(
                    "CAN profile {profile:?} is not compiled in"
                )))
            }
        };
    }
    options.engine = engine;
    options.motors = DmOptions {
        motor_count: args.motor_count,
        protocol: match args.motor_protocol.as_str() {
            "float" => MotorProtocol::Float,
            "dm-mit" => MotorProtocol::DmMit,
            _ => MotorProtocol::Auto,
        },
        motor_can_base: args.motor_can_base,
        slave_id: args.dm_slave_id,
        feedback_id: args.dm_feedback_id,
        motor_map: args.dm_motor,
        p_max: args.dm_p_max,
        v_max: args.dm_v_max,
        t_max: args.dm_t_max,
    };
    options.rate = args.rate;
    options.frames = args.frames;
    options.adc_input = args.adc_input;
    options.adc_voltage = args.adc_voltage;
    options.realtime = args.realtime;
    options.connect_timeout = args.connect_timeout;
    options.host_timeout = args.timeout;
    options.cancellation = Some(signals.cancelled.clone());
    options.verbose = args.verbose;
    let report = sim_host::worker::run(options).map_err(|error| LaunchError {
        code: error_code(&error),
        message: error.to_string(),
    })?;
    println!("Worker finished: frames={} sessions={} retries={} acknowledgements={} completions={} consumed_mask={} missing_mask={} final_time_ns={}", report.frames, report.sessions, report.retries, report.acknowledgements, report.completions, report.consumed_mask, report.missing_mask, report.final_time_ns);
    Ok(0)
}
