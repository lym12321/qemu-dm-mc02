//! Test-only actual motor::dm consumer. The fixture owns one 1 ms native step
//! per real control word; QEMU CAN timestamps are transport observations.
use serde_json::json;
use sim_host::{
    transports::fixed84::{CanFrameSink, CanStream},
    worker::{
        engine::{MujocoEngine, WorkerEngine},
        motor_protocol::{DmAdapter, DmOptions, MotorAction, MotorFeedback, MotorProtocol},
    },
};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::Path,
    time::{Duration, Instant},
};
fn diagnostic<T, E: std::fmt::Display>(r: Result<T, E>) -> Result<T, String> {
    r.map_err(|e| e.to_string())
}
fn require(ok: bool, message: impl Into<String>) -> Result<(), String> {
    if ok {
        Ok(())
    } else {
        Err(message.into())
    }
}
fn main() {
    if let Err(e) = execute() {
        eprintln!("MotorTest Rust consumer: {e}");
        std::process::exit(1);
    }
}
fn execute() -> Result<(), String> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    require(args.len() == 4, "expected UART CAN MODEL REPORT")?;
    let mut engine = diagnostic(MujocoEngine::new(Path::new(&args[2]), 1, 0.001, 10.0))?;
    let mut rows = Vec::new();
    let mut trace = Vec::new();
    let mut summaries = Vec::new();
    let mut stage = String::from("startup");
    let result = (|| -> Result<(), String> {
        let mut uart = diagnostic(UnixStream::connect(Path::new(&args[0])))?;
        diagnostic(uart.set_nonblocking(true))?;
        let can_socket = diagnostic(UnixStream::connect(Path::new(&args[1])))?;
        diagnostic(can_socket.set_nonblocking(true))?;
        let mut can = diagnostic(CanStream::new(can_socket, 256))?;
        let adapter = diagnostic(DmAdapter::new(DmOptions {
            motor_count: 1,
            protocol: MotorProtocol::DmMit,
            ..Default::default()
        }))?;
        diagnostic(engine.reset())?;
        println!("MOTOR_HOST_CONNECTED");
        diagnostic(std::io::stdout().flush())?;
        let words = |code| {
            let mut bytes = [0xff; 8];
            bytes[7] = code;
            bytes
        };
        let positive = [0x7f, 0xff, 0x7f, 0xf0, 0, 0, 8, 1];
        let negative = [0x7f, 0xff, 0x7f, 0xf0, 0, 0, 7, 0xfd];
        let mut expected = vec![words(0xfb)];
        expected.extend(vec![positive; 10]);
        expected.push(words(0xfc));
        expected.extend(vec![positive; 100]);
        expected.push(words(0xfd));
        expected.extend(vec![positive; 20]);
        expected.push(words(0xfc));
        expected.extend(vec![negative; 60]);
        expected.push(words(0xfd));
        let goldens = [
            ("disabled", [32768u64, 2048, 2048]),
            ("drive", [33349, 2348, 2049]),
            ("coast", [33580, 2348, 2048]),
            ("brake", [33920, 2048, 2045]),
        ];
        let mut required: BTreeSet<String> = [
            "control.rx",
            "reset.poweron",
            "bsp.init",
            "task.stack",
            "motor.feedback",
            "motor.units",
            "motor.can",
        ]
        .map(str::to_string)
        .into();
        for (phase, _) in goldens {
            for field in ["pos", "vel", "effort"] {
                required.insert(format!("motor.{phase}.{field}"));
            }
        }
        let mut buffer = Vec::new();
        let mut previous = None;
        let mut steps = 0u64;
        let mut started = false;
        let mut done = false;
        let drop_first = std::env::var("DM_MOTOR_DROP_FEEDBACK").unwrap_or_else(|_| "0".into());
        require(
            matches!(drop_first.as_str(), "0" | "1"),
            "DM_MOTOR_DROP_FEEDBACK must be0 or1",
        )?;
        let deadline = Instant::now() + Duration::from_secs(25);
        while !done && Instant::now() < deadline {
            let mut bytes = [0; 4096];
            match uart.read(&mut bytes) {
                Ok(0) => return Err("firmware UART disconnected".into()),
                Ok(n) => buffer.extend_from_slice(&bytes[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.to_string()),
            }
            require(buffer.len() <= 8192, "firmware UART output exceeded bound")?;
            while let Some(n) = buffer.iter().position(|b| *b == b'\n') {
                let line = diagnostic(std::str::from_utf8(&buffer[..n]))?
                    .trim()
                    .to_string();
                buffer.drain(..=n);
                if !line.starts_with("TTEST ") {
                    continue;
                }
                println!("{line}");
                let fields: Vec<_> = line.split_whitespace().collect();
                require(fields.len() == 5, "invalid firmware report")?;
                let actual = diagnostic(fields[3].parse::<u64>())?;
                let expected_field = diagnostic(fields[4].parse::<u64>())?;
                if matches!(fields[1], "PHASE" | "DONE") {
                    require(
                        diagnostic(fields[2].parse::<usize>())? == required.len()
                            && actual == 0
                            && expected_field == 1,
                        "firmware summary differs from required coverage",
                    )?;
                    summaries.push(fields[1].to_string());
                    done = fields[1] == "DONE";
                    continue;
                }
                let (status, name) = (fields[1], fields[2]);
                stage = name.into();
                rows.push(
                    json!({"status":status,"name":name,"actual":actual,"expected":expected_field}),
                );
                require(rows.len() <= 128, "firmware report count exceeded bound")?;
                match status {
                    "READY" => {
                        require(
                            name == "motor.start"
                                && !started
                                && (actual, expected_field) == (190, 1000),
                            "unexpected motor admission stage",
                        )?;
                        started = true;
                        diagnostic(uart.set_nonblocking(false))?;
                        diagnostic(uart.set_write_timeout(Some(Duration::from_secs(1))))?;
                        diagnostic(uart.write_all(&[0xa5]))?;
                        diagnostic(uart.set_nonblocking(true))?;
                    }
                    "FAIL" => return Err(format!("firmware failed: {name}")),
                    "SKIP" => require(
                        (name, actual, expected_field) == ("plant.physics", 0, 0),
                        "unexpected SKIP",
                    )?,
                    "PASS" => {
                        require(
                            required.contains(name),
                            format!("unexpected coverage {name}"),
                        )?;
                        if let Some(value) = match name {
                            "motor.feedback" => Some(195),
                            "motor.units" => Some(4),
                            "motor.can" => Some(0),
                            _ => None,
                        } {
                            require(
                                actual == value && expected_field == value,
                                "firmware motor summary mismatch",
                            )?;
                        }
                        for (phase, values) in goldens {
                            for (index, field) in ["pos", "vel", "effort"].into_iter().enumerate() {
                                if name == format!("motor.{phase}.{field}") {
                                    let golden = values[index];
                                    require(
                                        expected_field == golden
                                            && (actual == golden
                                                || (field == "vel" && actual + 1 == golden)),
                                        "actual firmware feedback differs from analytic golden",
                                    )?;
                                }
                            }
                        }
                    }
                    _ => return Err("unknown firmware test status".into()),
                }
            }
            for frame in diagnostic(can.receive())? {
                let ordinal = trace.len();
                require(
                    started && ordinal < expected.len(),
                    "unsolicited/extra motor command",
                )?;
                require(
                    frame.can_id() == 1
                        && frame.flags() == 0
                        && frame.dlc() == 8
                        && frame.data() == expected[ordinal],
                    format!("motor command mismatch at{ordinal}: {:?}", frame.data()),
                )?;
                let stamp = frame.timestamp_ns();
                require(
                    previous.is_none_or(|p| stamp > p),
                    "CAN command time did not increase",
                )?;
                previous = Some(stamp);
                let command = adapter
                    .decode(&frame)
                    .ok_or("adapter rejected admitted command")?;
                diagnostic(engine.command(command))?;
                if matches!(command.action, MotorAction::Mit(_)) {
                    diagnostic(engine.step(0.001))?;
                    steps += 1;
                }
                let actual = diagnostic(engine.motor_feedback(0))?;
                let (p, v, mut effort) = if steps <= 10 {
                    (0., 0., 0.)
                } else if steps <= 110 {
                    let n = (steps - 10) as f64;
                    let a = (30. / 4095.) * 6000.;
                    (a * 0.000001 * n * (n + 1.) / 2., a * 0.001 * n, 30. / 4095.)
                } else if steps <= 130 {
                    let v = 18000. / 4095.;
                    ((909. / 4095.) + v * 0.001 * (steps - 110) as f64, v, 0.)
                } else {
                    let n = (steps - 130) as f64;
                    let a = (-50. / 4095.) * 6000.;
                    (
                        (1269. / 4095.)
                            + (18000. / 4095.) * 0.001 * n
                            + a * 0.000001 * n * (n + 1.) / 2.,
                        18000. / 4095. + a * 0.001 * n,
                        -50. / 4095.,
                    )
                };
                if matches!(command.action, MotorAction::Enable(false)) {
                    effort = 0.;
                }
                require(
                    (actual.0 - p).abs() <= 1e-9
                        && (actual.1 - v).abs() <= 1e-9
                        && (actual.2 - effort).abs() <= 1e-9,
                    format!(
                        "rotor analytic mismatch at{ordinal}: {actual:?} expected{p}/{v}/{effort}"
                    ),
                )?;
                require(
                    (diagnostic(engine.time_s())? - steps as f64 * 0.001).abs() <= 1e-12,
                    "plant step clock drifted",
                )?;
                let feedback = diagnostic(adapter.feedback(MotorFeedback {
                    index: 0,
                    position: actual.0,
                    velocity: actual.1,
                    effort: actual.2,
                    timestamp_ns: stamp,
                }))?;
                if drop_first != "1" || ordinal != 0 {
                    diagnostic(can.send_frame(&feedback))?;
                }
                trace.push(json!({"can_time_ns":stamp,"plant_time_ns":steps*1_000_000,
                    "command":hex(frame.data()),"feedback":hex(feedback.data()),
                    "position":actual.0,"velocity":actual.1,"effort":actual.2}));
            }
            diagnostic(can.transport_mut().flush())?;
            std::thread::sleep(Duration::from_millis(1));
        }
        require(done, format!("firmware did not complete: {stage}"))?;
        let mut counts = BTreeMap::new();
        let mut skips = Vec::new();
        for row in &rows {
            if row["status"] == "PASS" {
                *counts
                    .entry(row["name"].as_str().unwrap().to_string())
                    .or_insert(0) += 1;
            }
            if row["status"] == "SKIP" {
                skips.push(row["name"].as_str().unwrap());
            }
        }
        require(
            counts == required.into_iter().map(|name| (name, 1)).collect()
                && skips == ["plant.physics"]
                && summaries == ["PHASE", "DONE"]
                && trace.len() == 195
                && steps == 190,
            "missing/duplicated/unexpected coverage or command",
        )?;
        Ok(())
    })();
    let cleanup = diagnostic(engine.close());
    let error = match (result, cleanup) {
        (Err(primary), Err(cleanup)) => Some(format!("{primary}; cleanup: {cleanup}")),
        (Err(error), _) | (_, Err(error)) => Some(error),
        (Ok(()), Ok(())) => None,
    };
    let report = json!({"result":if error.is_none(){"passed"}else{"failed"},"error":error,
        "last_stage":stage,"cases":rows,"trace":trace,"summaries":summaries,"plant_step_ns":1_000_000,
        "limits":["fixed rotor fixture","no global lockstep","no physical calibration","no realtime gate"]});
    diagnostic(std::fs::write(
        Path::new(&args[3]),
        diagnostic(serde_json::to_string_pretty(&report))? + "\n",
    ))?;
    error.map_or(Ok(()), Err)
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
