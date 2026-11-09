//! Test-only completed-state observations of the production shared CAN worker.
use clap::Parser;
use serde_json::json;
use sim_core::{Failure, Result};
use sim_host::worker::{can_graph::CanProfile, run_with_can_observer, Protocol, WorkerOptions};
use std::{
    cell::RefCell,
    fs::{self, OpenOptions},
    io::{BufWriter, Write},
    path::PathBuf,
    rc::Rc,
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
    #[arg(long)]
    ready: PathBuf,
}
fn io_error(error: impl std::error::Error + Send + Sync + 'static) -> Failure {
    Arc::new(error)
}
fn run(options: Options) -> Result<()> {
    let mut worker = WorkerOptions::new(options.cosim);
    worker.fdcan = Some(options.fdcan);
    worker.protocol = Protocol::V2;
    worker.can_profile = Some(CanProfile::FourWheel {
        model: options.model,
        delay_ns: 0,
        command_timeout_ns: 100_000_000,
        torque_per_amp: 0.02,
    });
    worker.realtime = true;
    worker.frames = 7500;
    worker.connect_timeout = Duration::from_secs(2);
    worker.host_timeout = Some(Duration::from_secs(12));
    let cancellation = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
        signal_hook::flag::register(signal, cancellation.clone()).map_err(io_error)?;
    }
    worker.cancellation = Some(cancellation);
    let output = Rc::new(RefCell::new(BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(options.trace)
            .map_err(io_error)?,
    )));
    let writer = output.clone();
    let result = run_with_can_observer(worker, move |cut| {
        let now = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
        let wall = now.tv_sec as u64 * 1_000_000_000 + now.tv_nsec as u64;
        let row = if cut.step_id == 0 {
            json!({"event":"reset", "wall_time_ns": wall,
                "session_id":cut.session.session_id, "timestamp_ns":cut.session.t_sim_ns,
                "qemu_origin_ns":cut.mapper.qemu_origin_ns,
                "worker_origin_ns":cut.mapper.worker_origin_ns})
        } else {
            let pose = cut.body_pose("chassis")?;
            let joints =
                ["wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr"].map(|name| cut.joint_state(name));
            let joints = joints.into_iter().collect::<Result<Vec<_>>>()?;
            let currents = (1..=4)
                .map(|id| cut.motor_current_raw(id))
                .collect::<Result<Vec<_>>>()?;
            let imu = cut.imu()?;
            json!({"event":"step", "wall_time_ns":wall,
                "timestamp_ns":cut.session.t_sim_ns, "plant_time_s":cut.native_time_s()?,
                "position_m":pose.position_m, "quaternion_wxyz":pose.quaternion_wxyz,
                "joint_position_rad":joints.iter().map(|j|j.position_rad).collect::<Vec<_>>(),
                "joint_velocity_rad_s":joints.iter().map(|j|j.velocity_rad_s).collect::<Vec<_>>(),
                "current_raw":currents, "gyro_rad_s":imu.gyro_rad_s, "accel_m_s2":imu.accel_m_s2})
        };
        let mut writer = writer.borrow_mut();
        serde_json::to_writer(&mut *writer, &row).map_err(io_error)?;
        writer.write_all(b"\n").map_err(io_error)?;
        if cut.step_id == 0 {
            writer.flush().map_err(io_error)?;
            fs::write(&options.ready, "Simulation session started\n").map_err(io_error)?;
        }
        Ok(())
    });
    let flush = output.borrow_mut().flush().map_err(io_error);
    match (result, flush) {
        (Err(primary), Err(cleanup)) => {
            eprintln!("chassis trace cleanup: {cleanup}");
            Err(primary)
        }
        (Err(primary), _) | (_, Err(primary)) => Err(primary),
        (Ok(report), Ok(())) => {
            println!(
                "{}",
                json!({"frames":report.frames,
            "final_time_ns":report.final_time_ns})
            );
            Ok(())
        }
    }
}
fn main() {
    if let Err(error) = run(Options::parse()) {
        eprintln!("chassis worker: {error}");
        std::process::exit(1);
    }
}
