//! Test-only stimulus for the actual BMI088 raw consumer; no private solver.
use serde_json::json;
use sim_core::{Error, Failure, Result, Session};
use sim_host::{
    can::CanFrame,
    devices::bmi088::imu_to_bmi088_units,
    step_wire::ImuSample,
    worker::can_graph::{CanGraph, CanProfile},
};
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

fn run() -> Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.len() != 2 || !matches!(args[1].as_str(), "rest" | "turn") {
        return Err(Arc::new(Error::Admission(
            "usage: native_mujoco MODEL rest|turn".into(),
        )));
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    let profile = CanProfile::FourWheel {
        model: PathBuf::from(&args[0]),
        delay_ns: 0,
        command_timeout_ns: 100_000_000,
        torque_per_amp: 0.02,
    };
    let admitted = profile.admit(false, deadline, None)?;
    let mut graph = CanGraph::new(&admitted, false, Session::new(1, 0)?, deadline, None)?;
    let result = (|| -> Result<_> {
        let count = if args[1] == "turn" { 1000 } else { 500 };
        for index in 0..count {
            let command = if index < 500 {
                vec![0; 8]
            } else {
                vec![0, 21, 0, 107, 0, 21, 0, 107]
            };
            graph.submit(
                "qemu",
                CanFrame::new(0x200, 0, None, command, index * 1_000_000)
                    .map_err(|e| e as Failure)?,
            )?;
            graph.reach((index + 1) * 1_000_000, deadline)?;
            graph.take_outputs()?;
        }
        let sample = graph.sample()?;
        let (gyro, accel) = imu_to_bmi088_units(&sample)?;
        let wire = ImuSample {
            gyro_dps: gyro,
            accel_g: accel,
            gyro_bias_dps: [0.0; 3],
            accel_bias_g: [0.0; 3],
            sensor_time_ns: count * 1_000_000,
            sample_id: 1,
        }
        .encode()
        .map_err(|e| e as Failure)?;
        Ok(
            json!({"gyro_rad_s":sample.gyro_rad_s, "accel_m_s2":sample.accel_m_s2,
            "native_time_s":graph.native_time_s()?, "target_ns":count * 1_000_000,
            "wire_hex":wire.iter().map(|b|format!("{b:02x}")).collect::<String>()}),
        )
    })();
    let cleanup = graph.close();
    match (result, cleanup) {
        (Err(primary), Err(error)) => {
            eprintln!("native MuJoCo fixture cleanup: {error}");
            Err(primary)
        }
        (Err(primary), _) | (_, Err(primary)) => Err(primary),
        (Ok(value), Ok(())) => {
            println!("{value}");
            Ok(())
        }
    }
}
fn main() {
    if let Err(error) = run() {
        eprintln!("native MuJoCo fixture: {error}");
        std::process::exit(1);
    }
}
