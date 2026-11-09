//! Actual Rust physics participant for the official Gazebo→ROS boundary gate.
use sim_core::{Error, Failure, Result, Session, StepContext};
use sim_host::physics::{
    gazebo::{GazeboBindings, GazeboPhysics},
    participant::PhysicsParticipant,
};
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

fn failure(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 2 {
        return Err(failure(
            "usage: gazebo_render_producer rendering.sdf finish-marker",
        ));
    }
    let finish = PathBuf::from(&args[1]);
    let deadline = Instant::now() + Duration::from_secs(10);
    let world = GazeboPhysics::from_sdf_path(
        PathBuf::from(&args[0]),
        GazeboBindings {
            cameras: vec![(
                "front".into(),
                "sensors".into(),
                "body".into(),
                "camera".into(),
            )],
            lidars: vec![(
                "scan".into(),
                "sensors".into(),
                "body".into(),
                "lidar".into(),
            )],
            ..Default::default()
        },
        &[],
    )?;
    let mut participant = PhysicsParticipant::new(world)?;
    let result = (|| {
        let origin = (1 << 63) + 17;
        participant.reset(Session::new(7, origin)?)?;
        let (mut camera, mut lidar) = (vec![], vec![]);
        let mut ticks = 0;
        loop {
            if Instant::now() >= deadline {
                return Err(failure(
                    "Rust rendering producer exceeded its host deadline",
                ));
            }
            if ticks < 900 {
                ticks += 1;
                participant.step(StepContext::new(
                    7,
                    ticks,
                    origin + ticks * 1_000_000,
                    1_000_000,
                )?)?;
            }
            let world = participant.physics();
            if participant.time_ns() != origin + world.time_ns()? {
                return Err(failure("participant core/native origins were mixed"));
            }
            if let Some(frame) = world.get_camera("front")? {
                if camera
                    .last()
                    .is_none_or(|stamp| frame.timestamp_ns > *stamp)
                {
                    if (frame.width, frame.height, frame.step, frame.data.len())
                        != (64, 48, 192, 9216)
                        || frame.frame_id != "sensors::body::camera"
                    {
                        return Err(failure("invalid native camera snapshot"));
                    }
                    let centre = (24 * 64 + 32) * 3;
                    let rgb = &frame.data[centre..centre + 3];
                    if !(rgb[0] > 100
                        && rgb[1] < 30
                        && rgb[2] < 30
                        && frame.data[..3].iter().all(|value| *value < 30))
                    {
                        return Err(failure("camera missed the independent red-box fixture"));
                    }
                    if camera.len() < 2 {
                        camera.push(frame.timestamp_ns);
                    }
                }
            }
            if let Some(scan) = world.get_lidar("scan")? {
                if lidar.last().is_none_or(|stamp| scan.timestamp_ns > *stamp) {
                    if scan.frame_id != "sensors::body::lidar"
                        || scan.ranges_m.len() != 65
                        || scan.intensities.len() != 65
                        || (scan.ranges_m[32] - 2.5).abs() > 0.02
                        || scan.ranges_m[0] != f64::INFINITY
                        || scan.ranges_m[64] != f64::INFINITY
                        || !scan.vertical_angle_step_rad.is_nan()
                    {
                        return Err(failure("lidar missed the independent box geometry"));
                    }
                    if lidar.len() < 2 {
                        lidar.push(scan.timestamp_ns);
                    }
                }
            }
            let finished = match std::fs::read(&finish) {
                Ok(bytes) if bytes == b"finish\n" => true,
                Ok(_) => return Err(failure("invalid finish marker")),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
                Err(error) => return Err(failure(format!("finish marker: {error}"))),
            };
            if finished && camera.len() == 2 && lidar.len() == 2 {
                let maps = std::fs::read_to_string("/proc/self/maps")
                    .map_err(|error| failure(format!("native process mapping: {error}")))?;
                if maps.contains("libpython") {
                    return Err(failure("Gazebo loaded libpython"));
                }
                return Ok((ticks, camera, lidar));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    })();
    let cleanup = participant.close();
    let (ticks, camera, lidar) = match result {
        Ok(value) => {
            cleanup?;
            value
        }
        Err(primary) => {
            if let Err(error) = cleanup {
                eprintln!("rendering cleanup: {error}");
            }
            return Err(primary);
        }
    };
    if Instant::now() >= deadline {
        return Err(failure("rendering cleanup exceeded host deadline"));
    }
    println!("{{\"production\":\"passed\",\"language\":\"rust\",\"ticks\":{ticks},\"camera_stamps_ns\":{camera:?},\"lidar_stamps_ns\":{lidar:?}}}");
    Ok(())
}
