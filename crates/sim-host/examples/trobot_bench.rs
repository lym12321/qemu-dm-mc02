//! Test-only observations around the production MuJoCo worker engine.
//! The shared worker retains transport, time, pacing and cleanup ownership.
use clap::Parser;
use serde_json::{json, Value};
use sim_core::{Error, Failure, Result};
use sim_host::{
    devices::bmi088::imu_to_bmi088_units,
    worker::{
        engine::{EngineOptions, EngineSample, MujocoEngine, WorkerEngine},
        motor_protocol::{MotorAction, MotorCommand, MotorProtocol},
        run_with_engine, Protocol, WorkerOptions,
    },
};
use std::{
    fs::{File, OpenOptions},
    io::{BufWriter, Write},
    path::PathBuf,
    sync::{atomic::AtomicBool, Arc},
    time::Duration,
};

#[derive(Parser)]
struct Options {
    #[arg(long)]
    cosim: PathBuf,
    #[arg(long)]
    fdcan: PathBuf,
    #[arg(long)]
    model: PathBuf,
    #[arg(long)]
    trace: PathBuf,
    #[arg(long, default_value_t = 14000)]
    frames: u64,
    #[arg(long, default_value_t = 50000)]
    max_events: usize,
}

struct BenchEngine {
    native: MujocoEngine,
    output: BufWriter<File>,
    max_events: usize,
    events: usize,
    initialized: bool,
}
fn io_failure(error: impl std::error::Error + Send + Sync + 'static) -> Failure {
    Arc::new(error)
}
impl BenchEngine {
    fn record(&mut self, event: &str, mut fields: Value) -> Result<()> {
        if self.events >= self.max_events {
            return Err(Arc::new(Error::State(format!(
                "business bench trace exceeded {} events",
                self.max_events
            ))));
        }
        // CLOCK_MONOTONIC is the same epoch used by the independent Python
        // QMP sampler; Instant's private origin cannot correlate processes.
        let now = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
        let fields = fields.as_object_mut().expect("trace fields are an object");
        fields.insert("event".into(), json!(event));
        fields.insert("plant_time_s".into(), json!(self.native.time_s()?));
        fields.insert(
            "wall_time_ns".into(),
            json!(now.tv_sec as u64 * 1_000_000_000 + now.tv_nsec as u64),
        );
        serde_json::to_writer(&mut self.output, fields).map_err(io_failure)?;
        self.output.write_all(b"\n").map_err(io_failure)?;
        self.events += 1;
        Ok(())
    }
}
impl WorkerEngine for BenchEngine {
    fn reset(&mut self) -> Result<()> {
        self.native.reset()?;
        // The shared graph establishes its initial session by resetting once.
        // Any later reset remains visible and invalidates the fixed benchmark.
        if self.initialized {
            self.record("reset", json!({}))?;
        }
        self.initialized = true;
        Ok(())
    }
    fn command(&mut self, command: MotorCommand) -> Result<()> {
        self.native.command(command)?;
        let index = command.index;
        match command.action {
            MotorAction::Mit(c) => self.record(
                "control",
                json!({
                    "index": index, "position": c.position, "speed": c.speed,
                    "kp": c.kp, "kd": c.kd, "torque": c.torque
                }),
            ),
            MotorAction::Enable(enabled) => {
                self.record("enabled", json!({"index": index, "enabled": enabled}))
            }
            MotorAction::Reset => self.record("clear-error", json!({"index": index})),
            MotorAction::Legacy(value) => {
                self.record("control", json!({"index": index, "value": value}))
            }
        }
    }
    fn step(&mut self, dt: f64) -> Result<EngineSample> {
        let sample = self.native.step(dt)?;
        let (position, velocity, effort) = sample.motors[0];
        let (gyro, accel) = imu_to_bmi088_units(&sample.imu)?;
        self.record(
            "step",
            json!({"dt": dt, "position": position, "velocity": velocity,
            "effort": effort, "gyro": gyro, "accel": accel}),
        )?;
        Ok(sample)
    }
    fn close(&mut self) -> Result<()> {
        let flush = self.output.flush().map_err(io_failure);
        let native = self.native.close();
        match (flush, native) {
            (Err(primary), Err(cleanup)) => {
                eprintln!("business bench native cleanup: {cleanup}");
                Err(primary)
            }
            (Err(primary), _) | (_, Err(primary)) => Err(primary),
            _ => Ok(()),
        }
    }
}
fn run(options: Options) -> Result<()> {
    if options.frames == 0 || options.max_events == 0 {
        return Err(Arc::new(Error::Admission(
            "benchmark needs finite positive frames and trace budget".into(),
        )));
    }
    let mut worker = WorkerOptions::new(options.cosim);
    worker.fdcan = Some(options.fdcan);
    worker.protocol = Protocol::V2;
    worker.engine = EngineOptions::Mujoco {
        model: options.model,
    };
    worker.motors.motor_count = 1;
    worker.motors.protocol = MotorProtocol::DmMit;
    worker.realtime = true;
    worker.frames = options.frames;
    worker.connect_timeout = Duration::from_secs(2);
    let cancellation = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
        signal_hook::flag::register(signal, cancellation.clone()).map_err(io_failure)?;
    }
    worker.cancellation = Some(cancellation);
    eprintln!("MuJoCo {}", mujoco_rs::mujoco_version());
    let report = run_with_engine(worker, |worker| {
        let EngineOptions::Mujoco { model } = &worker.engine else {
            unreachable!()
        };
        let native = MujocoEngine::new(model, 1, 1.0 / worker.rate, worker.motors.t_max)?;
        let output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&options.trace)
            .map_err(io_failure)?;
        Ok(Box::new(BenchEngine {
            native,
            output: BufWriter::new(output),
            max_events: options.max_events,
            events: 0,
            initialized: false,
        }))
    })?;
    println!(
        "{}",
        json!({"frames": report.frames, "final_time_ns": report.final_time_ns})
    );
    Ok(())
}
fn main() {
    if let Err(error) = run(Options::parse()) {
        eprintln!("business bench: {error}");
        std::process::exit(1);
    }
}
