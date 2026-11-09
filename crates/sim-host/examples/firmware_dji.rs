//! Test-only two-rotor consumer of the production CAN graph. Feedback loss is
//! injected at this fixture's physical stream boundary, outside the graph.
use serde_json::{json, Value};
use sim_core::Session;
use sim_host::{
    can::CanFrame,
    devices::dji::protocol::encode_feedback,
    transports::fixed84::{CanFrameSink, CanStream},
    worker::can_graph::{CanGraph, CanProfile, RotorProfile},
};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    os::unix::net::UnixStream,
    path::Path,
    time::{Duration, Instant},
};
const STEP_NS: u64 = 1_000_000;
const MAX_CATCHUP: u64 = 1000;
const MAX_RECORDS: usize = 10_000;
const STAGES: &[(&str, u32, u32)] = &[
    ("dji.vector", 0x201, 0x205),
    ("dji.start", 0, 0),
    ("dji.positive", 4096, 0xfffff000),
    ("dji.negative", 0xfffff000, 4096),
    ("dji.disable", 0, 4096),
    ("dji.enable", 4096, 4096),
    ("dji.drop", 0x201, 50),
    ("dji.recover", 0x201, 0),
    ("dji.stop", 0, 0),
];
const WIRE_TARGETS: &[(&str, [i16; 2])] = &[
    ("dji.positive", [4096, -4096]),
    ("dji.negative", [-4096, 4096]),
    ("dji.disable", [0, 4096]),
    ("dji.enable", [4096, 4096]),
    ("dji.drop", [0, 4096]),
    ("dji.recover", [4096, 4096]),
    ("dji.stop", [0, 0]),
];
const EXACT_EXPECTED: &[(&str, u32)] = &[
    ("control.rx", 0),
    ("reset.poweron", 0x800000),
    ("bsp.init", 0),
    ("dji.vector.id1", 2048),
    ("dji.vector.id5", 4096),
    ("dji.units.id1", 0),
    ("dji.units.id5", 0),
    ("dji.vector.disabled", 0),
    ("dji.online", 1),
    ("dji.positive.id1", 4096),
    ("dji.positive.id5", 0xfffff000),
    ("dji.positive.feedback", 4096),
    ("dji.negative.id1", 0xfffff000),
    ("dji.negative.id5", 4096),
    ("dji.negative.feedback", 0xfffff000),
    ("dji.disable.ignored", 0),
    ("dji.disable.id5", 4096),
    ("dji.enable.id1", 4096),
    ("dji.enable.id5", 4096),
    ("dji.drop.output", 0),
    ("dji.drop.id5", 4096),
    ("dji.recover.feedback", 4096),
    ("dji.recover.id1", 4096),
    ("dji.recover.id5", 4096),
    ("dji.stop", 0),
    ("dji.can", 0),
];
fn checked<T, E: std::fmt::Display>(r: Result<T, E>) -> Result<T, String> {
    r.map_err(|e| e.to_string())
}
fn require(ok: bool, message: impl Into<String>) -> Result<(), String> {
    if ok {
        Ok(())
    } else {
        Err(message.into())
    }
}
fn push(rows: &mut Vec<Value>, row: Value) -> Result<(), String> {
    require(
        rows.len() < MAX_RECORDS,
        "DJI fixture observation capacity exhausted",
    )?;
    rows.push(row);
    Ok(())
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
struct Fixture {
    graph: CanGraph,
    can: CanStream<UnixStream>,
    stage: String,
    stage_index: usize,
    phase_start: BTreeMap<String, u64>,
    wire_seen: BTreeMap<(String, u32), BTreeSet<i16>>,
    trace: Vec<Value>,
    deliveries: Vec<Value>,
    previous: u64,
    time: u64,
    steps: u64,
    deadline: Instant,
}
impl Fixture {
    fn new(socket: UnixStream, deadline: Instant) -> Result<Self, String> {
        let profile = CanProfile::Rotor(RotorProfile {
            motor_ids: vec![1, 5],
            ..Default::default()
        });
        let admitted = checked(profile.admit(true, deadline, None))?;
        let graph = checked(CanGraph::new(
            &admitted,
            true,
            checked(Session::new(1, 0))?,
            deadline,
            None,
        ))?;
        checked(socket.set_nonblocking(true))?;
        Ok(Self {
            graph,
            can: checked(CanStream::new(socket, 256))?,
            stage: "startup".into(),
            stage_index: 0,
            phase_start: BTreeMap::new(),
            wire_seen: BTreeMap::new(),
            trace: vec![],
            deliveries: vec![],
            previous: 0,
            time: 0,
            steps: 0,
            deadline,
        })
    }
    fn feedback(&mut self, literal: bool) -> Result<(), String> {
        for (destination, frame) in checked(self.graph.take_outputs())? {
            if destination != "qemu" {
                continue;
            }
            require(
                matches!(frame.can_id(), 0x201 | 0x205),
                "unexpected fixture feedback",
            )?;
            let dropped = !literal
                && (matches!(self.stage.as_str(), "startup" | "dji.vector")
                    || (self.stage == "dji.drop" && frame.can_id() == 0x201));
            push(
                &mut self.deliveries,
                json!({"phase":self.stage,
                "source":if literal {"literal-vector".into()} else {format!("motor{}",frame.can_id()-0x200)},
                "destination":"transport.qemu", "can_id":frame.can_id(), "flags":frame.flags(),
                "dlc":frame.dlc(), "data":hex(frame.data()), "original_time_ns":frame.timestamp_ns(),
                "due_ns":self.time,"delivered_time_ns":self.time,"dropped":dropped}),
            )?;
            if !dropped {
                checked(self.can.send_frame(&frame))?;
            }
        }
        Ok(())
    }
    fn finish_stage(&self) -> Result<(), String> {
        if let Some((_, values)) = WIRE_TARGETS.iter().find(|(phase, _)| *phase == self.stage) {
            for (id, value) in [0x200, 0x1ff].into_iter().zip(values) {
                require(
                    self.wire_seen
                        .get(&(self.stage.clone(), id))
                        .is_some_and(|v| v.contains(value)),
                    format!("missing actual {} command {id:#x} slot={value}", self.stage),
                )?;
            }
        }
        if self.stage == "dji.drop" {
            let deadline = self.phase_start[&self.stage] + 51 * STEP_NS;
            require(
                self.trace.iter().any(|r| {
                    r["phase"] == self.stage
                        && r["can_id"] == 0x200
                        && r["slots"][0] == 0
                        && r["can_time_ns"].as_u64().is_some_and(|t| t > deadline)
                }),
                "missing actual timeout zero beyond51ms drop interval",
            )?;
            for (id, dropped) in [(0x201, true), (0x205, false)] {
                require(
                    self.deliveries.iter().any(|r| {
                        r["phase"] == self.stage && r["can_id"] == id && r["dropped"] == dropped
                    }),
                    "targeted feedback drop lacks independent ID5 control",
                )?;
            }
        }
        Ok(())
    }
    fn ready(&mut self, name: &str, actual: u32, expected: u32) -> Result<(), String> {
        require(
            STAGES.get(self.stage_index) == Some(&(name, actual, expected)),
            "unexpected DJI admission stage",
        )?;
        self.finish_stage()?;
        self.stage = name.into();
        self.phase_start
            .insert(name.into(), self.previous.max(self.time));
        self.stage_index += 1;
        if name == "dji.vector" {
            for (id, encoder, rpm, current, temperature, literal) in [
                (1, 2048, -192, -8192, 42, "0800ff40e0002a00"),
                (5, 4096, 192, 8192, 43, "100000c020002b00"),
            ] {
                let frame = checked(encode_feedback(
                    id,
                    encoder,
                    rpm,
                    current,
                    temperature,
                    self.time,
                ))?;
                require(
                    hex(frame.data()) == literal,
                    "DJI producer differs from literal vector",
                )?;
                checked(self.graph.submit("socketcan", frame))?;
            }
            checked(self.graph.reach(self.time, self.deadline))?;
            self.feedback(true)?;
        }
        Ok(())
    }
    fn ingress(&mut self, frame: CanFrame, remaining: u64) -> Result<(), String> {
        require(
            frame.timestamp_ns() >= self.previous,
            "QEMU CAN timestamp moved backwards",
        )?;
        require(
            matches!(frame.can_id(), 0x200 | 0x1ff)
                && frame.flags() == 0
                && frame.dlc() == 8
                && frame.data().len() == 8,
            "unexpected real DJI command shape",
        )?;
        let slots: Vec<_> = frame
            .data()
            .chunks_exact(2)
            .map(|b| i16::from_be_bytes([b[0], b[1]]))
            .collect();
        require(
            matches!(slots[0], -4096 | 0 | 4096) && slots[1..] == [0, 0, 0],
            "unexpected real DJI command slots",
        )?;
        let target = frame.timestamp_ns() / STEP_NS * STEP_NS;
        let catchup = target.saturating_sub(self.time) / STEP_NS;
        require(
            catchup <= remaining,
            "QEMU CAN gap exceeds bounded fixture catch-up",
        )?;
        for _ in 0..catchup {
            self.time += STEP_NS;
            checked(self.graph.reach(self.time, self.deadline))?;
            self.steps += 1;
            self.feedback(false)?;
        }
        self.previous = frame.timestamp_ns();
        self.wire_seen
            .entry((self.stage.clone(), frame.can_id()))
            .or_default()
            .insert(slots[0]);
        push(
            &mut self.trace,
            json!({"phase":self.stage,"can_id":frame.can_id(),"flags":frame.flags(),
            "dlc":frame.dlc(),"can_time_ns":frame.timestamp_ns(),"plant_time_ns":self.time,
            "slots":slots,"data":hex(frame.data())}),
        )?;
        checked(self.graph.submit("qemu", frame))
    }
}
fn check_case(status: &str, name: &str, actual: u32, expected: u32) -> Result<(), String> {
    if status == "SKIP" {
        return require(
            (name, actual, expected) == ("plant.physics", 0, 0),
            "unexpected SKIP",
        );
    }
    require(
        status == "PASS",
        format!("firmware failed/unexpected coverage: {name}"),
    )?;
    let ok = match name {
        "task.stack" => actual >= 64 && expected == 64,
        "dji.drop.age" => actual > 50 && expected == 51,
        "dji.drop.app_age" => actual < 50 && expected == 50,
        "dji.drop.frozen" => actual == expected && actual != 0,
        _ => EXACT_EXPECTED
            .iter()
            .any(|(n, v)| *n == name && actual == *v && expected == *v),
    };
    require(ok, format!("firmware report fields disagree: {name}"))
}
fn main() {
    if let Err(e) = execute() {
        eprintln!("DjiTest Rust consumer: {e}");
        std::process::exit(1);
    }
}
fn execute() -> Result<(), String> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    require(args.len() == 3, "expected UART CAN REPORT")?;
    let deadline = Instant::now() + Duration::from_secs(25);
    let mut uart = checked(UnixStream::connect(Path::new(&args[0])))?;
    checked(uart.set_nonblocking(true))?;
    let socket = checked(UnixStream::connect(Path::new(&args[1])))?;
    let mut fixture = Fixture::new(socket, deadline)?;
    let required: BTreeSet<_> = EXACT_EXPECTED
        .iter()
        .map(|(n, _)| *n)
        .chain([
            "task.stack",
            "dji.drop.frozen",
            "dji.drop.age",
            "dji.drop.app_age",
        ])
        .collect();
    let mut rows = vec![];
    let mut summaries = vec![];
    let mut counts = BTreeMap::<String, usize>::new();
    let mut skips = vec![];
    let result = (|| -> Result<(), String> {
        println!("DJI_HOST_CONNECTED");
        checked(std::io::stdout().flush())?;
        let mut buffer = vec![];
        let mut done = false;
        while !done && Instant::now() < deadline {
            let start = fixture.steps;
            for frame in checked(fixture.can.receive())? {
                fixture.ingress(frame, MAX_CATCHUP - (fixture.steps - start))?;
            }
            let mut bytes = [0; 4096];
            match uart.read(&mut bytes) {
                Ok(0) => return Err("firmware UART disconnected".into()),
                Ok(n) => buffer.extend_from_slice(&bytes[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.to_string()),
            }
            require(buffer.len() <= 8192, "firmware UART output exceeded bound")?;
            while let Some(n) = buffer.iter().position(|b| *b == b'\n') {
                let line = checked(std::str::from_utf8(&buffer[..n]))?
                    .trim()
                    .to_owned();
                buffer.drain(..=n);
                if !line.starts_with("TTEST ") {
                    continue;
                }
                println!("{line}");
                let fields: Vec<_> = line.split_whitespace().collect();
                require(fields.len() == 5, "invalid firmware report")?;
                let a = checked(fields[3].parse::<u32>())?;
                let e = checked(fields[4].parse::<u32>())?;
                if matches!(fields[1], "PHASE" | "DONE") {
                    require(
                        checked(fields[2].parse::<usize>())? == required.len() && a == 0 && e == 1,
                        "firmware summary differs from required coverage",
                    )?;
                    summaries.push(fields[1].to_owned());
                    done = fields[1] == "DONE";
                    continue;
                }
                let (status, name) = (fields[1], fields[2]);
                push(
                    &mut rows,
                    json!({"status":status,"name":name,"actual":a,"expected":e}),
                )?;
                if status == "READY" {
                    fixture.ready(name, a, e)?;
                    checked(uart.set_nonblocking(false))?;
                    checked(uart.set_write_timeout(Some(Duration::from_secs(1))))?;
                    checked(uart.write_all(&[0xa5]))?;
                    checked(uart.set_nonblocking(true))?;
                    continue;
                }
                check_case(status, name, a, e)?;
                if status == "SKIP" {
                    skips.push(name.to_owned());
                    continue;
                }
                *counts.entry(name.into()).or_default() += 1;
            }
            checked(fixture.can.transport_mut().flush())?;
            std::thread::sleep(Duration::from_millis(1));
        }
        require(done, "firmware did not complete")?;
        fixture.finish_stage()?;
        require(
            fixture.stage_index == STAGES.len(),
            "missing DJI admission stages",
        )?;
        for id in [1, 5] {
            require(
                checked(fixture.graph.motor_state(id))?.rejected_commands == 0,
                "virtual M3508 rejected a real driver command",
            )?;
        }
        require(
            counts.len() == required.len()
                && counts.values().all(|n| *n == 1)
                && skips == ["plant.physics"]
                && summaries == ["PHASE", "DONE"],
            "missing/duplicated firmware coverage",
        )?;
        Ok(())
    })();
    let cleanup = checked(fixture.graph.close());
    let error = match (result, cleanup) {
        (Ok(()), Ok(())) => None,
        (Err(e), Ok(())) | (Ok(()), Err(e)) => Some(e),
        (Err(e), Err(c)) => Some(format!("{e}; cleanup failed: {c}")),
    };
    let report = json!({"result":if error.is_none(){"passed"}else{"failed"},"error":error,
        "last_stage":fixture.stage,"cases":rows,"summaries":summaries,
        "missing":required.into_iter().filter(|n|!counts.contains_key(*n)).collect::<Vec<_>>(),
        "trace":fixture.trace,"deliveries":fixture.deliveries,"steps":fixture.steps,
        "plant_time_ns":fixture.time,"host_quantum_ns":STEP_NS,"phase_start_ns":fixture.phase_start,
        "max_catchup_steps":MAX_CATCHUP,"observation_bound":MAX_RECORDS,
        "delivery_scope":"physical feedback gateway to QEMU; incoming wire commands in trace",
        "limits":["standalone QEMU epoch=0","fixed two-rotor fixture","1ms host sample clock",
            "no global lockstep","no physical calibration","no realtime gate"]});
    checked(std::fs::write(
        Path::new(&args[2]),
        checked(serde_json::to_vec_pretty(&report))?,
    ))?;
    match error {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn command(id: u32, stamp: u64, value: i16) -> CanFrame {
        let mut bytes = vec![0; 8];
        bytes[..2].copy_from_slice(&value.to_be_bytes());
        CanFrame::new(id, 0, Some(8), bytes, stamp).unwrap()
    }
    #[test]
    fn shared_cut_literals_and_physical_drop_use_the_production_graph() {
        let (socket, mut peer) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let mut fixture = Fixture::new(socket, Instant::now() + Duration::from_secs(2)).unwrap();
        fixture
            .ingress(command(0x200, 1_010_000, 0), MAX_CATCHUP)
            .unwrap();
        fixture
            .ingress(command(0x1ff, 1_020_000, 0), MAX_CATCHUP)
            .unwrap();
        assert_eq!(fixture.steps, 1);
        fixture.ready("dji.vector", 0x201, 0x205).unwrap();
        fixture.can.transport_mut().flush().unwrap();
        let mut bytes = [0; 168];
        peer.read_exact(&mut bytes).unwrap();
        let frames: Vec<_> = bytes
            .chunks_exact(84)
            .map(|b| sim_host::transports::fixed84::decode(b).unwrap())
            .collect();
        assert_eq!(
            frames
                .iter()
                .map(|f| (f.can_id(), hex(f.data())))
                .collect::<Vec<_>>(),
            [
                (0x201, "0800ff40e0002a00".into()),
                (0x205, "100000c020002b00".into())
            ]
        );
        fixture.ready("dji.start", 0, 0).unwrap();
        fixture.stage = "dji.drop".into();
        fixture.phase_start.insert("dji.drop".into(), fixture.time);
        fixture
            .ingress(command(0x200, 2_010_000, 0), MAX_CATCHUP)
            .unwrap();
        fixture
            .ingress(command(0x1ff, 2_020_000, 4096), MAX_CATCHUP)
            .unwrap();
        assert!(fixture.finish_stage().unwrap_err().contains("beyond51ms"));
        fixture
            .ingress(command(0x200, 60_010_000, 0), MAX_CATCHUP)
            .unwrap();
        fixture.finish_stage().unwrap();
        let before = fixture.steps;
        assert!(fixture
            .ingress(command(0x200, 59_010_000, 0), MAX_CATCHUP)
            .unwrap_err()
            .contains("backwards"));
        assert!(fixture
            .ingress(command(0x200, 1_100_000_000, 0), MAX_CATCHUP)
            .unwrap_err()
            .contains("catch-up"));
        assert_eq!(fixture.steps, before);
        fixture.graph.close().unwrap();
    }
    #[test]
    fn report_fields_keep_strict_timeout_and_only_the_physics_skip() {
        for (name, actual, expected, valid) in [
            ("dji.drop.age", 86, 51, true),
            ("dji.drop.age", 50, 51, false),
            ("dji.drop.app_age", 3, 50, true),
            ("dji.drop.app_age", 50, 50, false),
            ("dji.drop.frozen", 0, 0, false),
            ("dji.positive.id5", 0xfffff000, 0xfffff000, true),
            ("dji.positive.id5", 4096, 4096, false),
            ("task.stack", 63, 64, false),
        ] {
            assert_eq!(
                check_case("PASS", name, actual, expected).is_ok(),
                valid,
                "{name}"
            );
        }
        assert!(check_case("SKIP", "plant.physics", 0, 0).is_ok());
        assert!(check_case("SKIP", "dji.drop.output", 0, 0).is_err());
        assert!(check_case("FAIL", "control.rx", 0, 0).is_err());
    }
}
