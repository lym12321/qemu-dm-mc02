#![cfg(all(
    target_os = "linux",
    feature = "referee",
    feature = "machine",
    feature = "scene"
))]
use sim_core::{Failure, Result, Session, StepContext};
use sim_host::{
    config::yaml::parse_document,
    devices::referee::{config::parse_options, decode_frame, RefereeEngine, RefereeUartEndpoint},
    launch,
    native_process::{NativeProcess, ProcessOptions},
    qmp::{commands, QmpSession},
    transports::stream::StreamTransport,
};
use std::{
    cell::{Cell, RefCell},
    collections::BTreeMap,
    ffi::OsString,
    fs,
    io::{self, Read, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    rc::Rc,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
fn err(message: impl Into<String>) -> Failure {
    Arc::new(io::Error::other(message.into()))
}
fn ensure(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(err(message))
    }
}
struct BudgetIo {
    socket: UnixStream,
    budget: Rc<Cell<usize>>,
    sent: Rc<RefCell<Vec<u8>>>,
}
impl Read for BudgetIo {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.socket.read(bytes)
    }
}
impl Write for BudgetIo {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let n = bytes.len().min(self.budget.get());
        if n == 0 {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        let n = self.socket.write(&bytes[..n])?;
        if self.sent.borrow().len() + n > 164 {
            return Err(io::Error::other(
                "fixed native referee observation exceeds 164 bytes",
            ));
        }
        self.sent.borrow_mut().extend_from_slice(&bytes[..n]);
        self.budget.set(self.budget.get() - n);
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
fn check_wire(mut wire: &[u8], batch: usize) -> Result<()> {
    let expected = [
        [
            "2101000807060504030201",
            "03026400c80003006400500005",
            "000000000000000028000a001400",
            "050900070b0d00fe",
        ],
        [
            "3103000807060504030201",
            "03024600c80003006400780006",
            "000000000000000028000c001900",
            "08110013171d0080",
        ],
    ];
    ensure(wire.len() == 82, "snapshot must contain exactly 82 bytes")?;
    for (i, command) in [1, 0x201, 0x202, 0x204].into_iter().enumerate() {
        let n = u16::from_le_bytes([wire[1], wire[2]]) as usize + 9;
        let frame = decode_frame(&wire[..n])?;
        let hex = frame
            .payload
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        ensure(
            (frame.command_id, frame.sequence, hex.as_str())
                == (command, (batch * 4 + i) as u8, expected[batch][i]),
            "snapshot differs from fixed native firmware literals",
        )?;
        wire = &wire[n..];
    }
    Ok(())
}
#[test]
#[ignore = "requires retained RefereeTestDebug ELF and canonical native QEMU"]
fn actual_uart7_dma_parser_consumes_initial_and_retained_partial_snapshot() {
    let cancelled = Arc::new(AtomicBool::new(false));
    let signals: Vec<_> = [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM]
        .into_iter()
        .map(|signal| signal_hook::flag::register(signal, cancelled.clone()).unwrap())
        .collect();
    let asset = |name| fs::canonicalize(std::env::var_os(name).expect(name)).unwrap();
    let qemu = asset("SIM_NATIVE_QEMU");
    let firmware = asset("SIM_NATIVE_REFEREE_ELF");
    launch::check_machine(&qemu).unwrap();
    launch::readable_file(&firmware, "RefereeTest ELF").unwrap();
    let deadline = Instant::now() + Duration::from_secs(20);
    let mut argv: Vec<OsString> = vec![
        qemu.into_os_string(),
        "-machine".into(),
        "dm-mc02".into(),
        "-kernel".into(),
        firmware.into_os_string(),
        "-display".into(),
        "none".into(),
        "-monitor".into(),
        "none".into(),
        "-S".into(),
        "-qmp".into(),
        "unix:{qmp},server=on,wait=off".into(),
    ];
    // Match the canonical DM-MC02 serial slot map; unused UART/CAN/co-sim stay null.
    for slot in [
        None,
        Some("uart1"),
        None,
        None,
        None,
        Some("uart7"),
        None,
        None,
        None,
        None,
    ] {
        argv.push("-serial".into());
        argv.push(slot.map_or_else(
            || "null".into(),
            |name| format!("unix:{{{name}}},server=on,wait=off").into(),
        ));
    }
    let mut owner = NativeProcess::start(ProcessOptions {
        argv,
        sockets: vec!["qmp".into(), "uart1".into(), "uart7".into()],
        wait_sockets: None,
        startup_timeout: Duration::from_secs(5),
        host_deadline: deadline,
    })
    .unwrap();
    let pid = owner.id().unwrap();
    let directory = owner.directory().unwrap().to_path_buf();
    println!(
        "NATIVE_OWNER {}",
        serde_json::json!({"pid":pid,"directory":directory})
    );
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let mut qmp = runtime
        .block_on(QmpSession::connect_uds(
            owner.socket("qmp").unwrap(),
            deadline,
        ))
        .unwrap();
    let uart1 = UnixStream::connect(owner.socket("uart1").unwrap()).unwrap();
    uart1.set_nonblocking(true).unwrap();
    let uart7 = UnixStream::connect(owner.socket("uart7").unwrap()).unwrap();
    uart7.set_nonblocking(true).unwrap();
    let mut control = StreamTransport::new(uart1, 1).unwrap();
    let budget = Rc::new(Cell::new(usize::MAX));
    let sent = Rc::new(RefCell::new(vec![]));
    let stream = Rc::new(RefCell::new(
        StreamTransport::new(
            BudgetIo {
                socket: uart7,
                budget: budget.clone(),
                sent: sent.clone(),
            },
            1,
        )
        .unwrap(),
    ));
    let sender = stream.clone();
    let mut endpoint = RefereeUartEndpoint::new(
        move |bytes| {
            sender
                .borrow_mut()
                .send(bytes)
                .map_err(|error| error as Failure)
        },
        0,
    );
    let configuration = parse_options(
        &parse_document(include_bytes!("fixtures/referee_options.yaml")).unwrap(),
        "referee",
    )
    .unwrap()
    .configuration;
    let mut engine = RefereeEngine::new(configuration).unwrap();
    let result = (|| -> Result<serde_json::Value> {
        owner.check().map_err(|error| error as Failure)?;
        runtime
            .block_on(qmp.command(commands::cont {}, deadline))
            .map_err(|error| error as Failure)?;
        let mut lines = String::new();
        let mut cases: BTreeMap<String, (String, u32, u32)> = BTreeMap::new();
        let mut stages = 0;
        let mut summaries = vec![];
        let mut all_wires = vec![];
        let mut total_bytes = 0;
        loop {
            ensure(
                !cancelled.load(Ordering::Relaxed),
                "referee consumer interrupted",
            )?;
            owner.check().map_err(|error| error as Failure)?;
            control.read_once().map_err(|error| error as Failure)?;
            let count = control.buffered_bytes();
            total_bytes += count;
            ensure(total_bytes <= 128000, "firmware UART exceeds 128KiB")?;
            if count != 0 {
                let bytes = control
                    .next_record(count)
                    .map_err(|error| error as Failure)?
                    .unwrap();
                lines.push_str(
                    std::str::from_utf8(&bytes).map_err(|error| Arc::new(error) as Failure)?,
                );
            }
            ensure(
                lines.len() <= 12000,
                "firmware line buffer exceeds 12000 bytes",
            )?;
            while let Some(end) = lines.find('\n') {
                let line = lines.drain(..=end).collect::<String>();
                let parts: Vec<_> = line.split_whitespace().collect();
                if parts.first() != Some(&"TTEST") {
                    continue;
                }
                println!("{}", line.trim());
                ensure(parts.len() == 5, "malformed actual firmware report")?;
                if matches!(parts[1], "PHASE" | "DONE") {
                    ensure(
                        parts[2..] == ["17", "0", "1"],
                        "actual firmware summary differs",
                    )?;
                    summaries.push(parts[1].to_owned());
                    if parts[1] == "DONE" {
                        ensure(
                            stages == 3 && summaries == ["PHASE", "DONE"],
                            "missing referee handshake or summary",
                        )?;
                        ensure(
                            cases.len() == 18,
                            "unexpected/missing actual firmware cases",
                        )?;
                        for name in [
                            "control.rx",
                            "reset.poweron",
                            "bsp.init",
                            "referee.executor",
                            "referee.partial.stable",
                            "referee.uart",
                            "task.stack",
                            "referee.initial.fresh",
                            "referee.initial.game",
                            "referee.initial.robot",
                            "referee.initial.power",
                            "referee.initial.buff",
                            "referee.complete.fresh",
                            "referee.complete.game",
                            "referee.complete.robot",
                            "referee.complete.power",
                            "referee.complete.buff",
                        ] {
                            ensure(
                                cases.get(name).is_some_and(|value| value.0 == "PASS"),
                                "required actual firmware case missing",
                            )?;
                        }
                        ensure(
                            cases.get("plant.physics") == Some(&("SKIP".into(), 0, 0)),
                            "physical calibration SKIP missing",
                        )?;
                        ensure(
                            endpoint.next_sequence() == 8
                                && !stream.borrow().tx_pending()
                                && *sent.borrow() == all_wires,
                            "original suffix not drained exactly",
                        )?;
                        runtime
                            .block_on(qmp.command(commands::stop {}, deadline))
                            .map_err(|error| error as Failure)?;
                        let watchdog = runtime
                            .block_on(qmp.command(
                                commands::qom_get {
                                    path: "/machine".into(),
                                    property: "iwdg-diagnostics".into(),
                                },
                                deadline,
                            ))
                            .map_err(|error| error as Failure)?;
                        let diagnostics = watchdog
                            .as_str()
                            .ok_or_else(|| err("missing watchdog diagnostics"))?;
                        ensure(
                            diagnostics.contains("timeouts=0")
                                && diagnostics.contains("window-violations=0"),
                            "watchdog event during actual referee consumer",
                        )?;
                        owner.check().map_err(|error| error as Failure)?;
                        return Ok(serde_json::json!({
                            "cases": cases, "stages": stages, "summaries": summaries,
                            "wire_sent_hex": all_wires.iter().map(|b|format!("{b:02x}")).collect::<String>(),
                            "next_sequence": endpoint.next_sequence(), "watchdog": diagnostics,
                            "uart_bytes": total_bytes, "qemu_pid": pid, "directory": directory,
                        }));
                    }
                    continue;
                }
                let actual = parts[3]
                    .parse::<u32>()
                    .map_err(|error| Arc::new(error) as Failure)?;
                let expected = parts[4]
                    .parse::<u32>()
                    .map_err(|error| Arc::new(error) as Failure)?;
                if parts[1] == "READY" {
                    let required = [
                        ("referee.initial", 82, 0),
                        ("referee.partial", 19, 82),
                        ("referee.complete", 82, 1),
                    ];
                    ensure(
                        stages < 3 && (parts[2], actual, expected) == required[stages],
                        "unexpected referee native stage",
                    )?;
                    match stages {
                        0 => {
                            engine.reset(Session::new(1, 0)?)?;
                            let wire = endpoint.send_state(&engine.get_state()?)?;
                            check_wire(&wire, 0)?;
                            all_wires.extend_from_slice(&wire);
                            ensure(
                                sent.borrow().len() == 82 && !stream.borrow().tx_pending(),
                                "initial batch did not drain",
                            )?;
                        }
                        1 => {
                            engine.step(StepContext::new(1, 1, 1_000_000_000, 1_000_000_000)?)?;
                            budget.set(19);
                            let wire = endpoint.send_state(&engine.get_state()?)?;
                            check_wire(&wire, 1)?;
                            all_wires.extend_from_slice(&wire);
                            ensure(
                                sent.borrow().len() == 101
                                    && stream.borrow().pending_records() == 1
                                    && *sent.borrow() == all_wires[..101],
                                "actual19byteprefix not retained",
                            )?;
                        }
                        _ => {
                            ensure(
                                sent.borrow().len() == 101,
                                "suffix sent during100ms guest stability observation",
                            )?;
                            budget.set(usize::MAX);
                            ensure(
                                stream
                                    .borrow_mut()
                                    .flush()
                                    .map_err(|error| error as Failure)?,
                                "retained suffix did not drain",
                            )?;
                            ensure(
                                sent.borrow().len() == 164,
                                "expected exact63byte retained suffix",
                            )?;
                        }
                    }
                    stages += 1;
                    control.send(vec![0xa5]).map_err(|error| error as Failure)?;
                    ensure(
                        control.flush().map_err(|error| error as Failure)?,
                        "control ack must drain",
                    )?;
                } else {
                    ensure(
                        cases
                            .insert(parts[2].to_owned(), (parts[1].to_owned(), actual, expected))
                            .is_none(),
                        "duplicate firmware report",
                    )?;
                    if parts[2] == "plant.physics" {
                        ensure(
                            parts[1] == "SKIP" && actual == 0 && expected == 0,
                            "invalid physical SKIP",
                        )?;
                    } else {
                        ensure(parts[1] == "PASS", "actual firmware reported failure")?;
                        let valid = match parts[2] {
                            "task.stack" => expected == 64 && actual >= 64,
                            "reset.poweron" => actual == 8388608 && expected == 8388608,
                            name if name.ends_with(".fresh") || name.ends_with(".stable") => {
                                actual == 1 && expected == 1
                            }
                            _ => actual == 0 && expected == 0,
                        };
                        ensure(valid, "actual firmware fields differ")?;
                    }
                }
            }
            if count == 0 {
                std::thread::sleep(Duration::from_millis(1));
            }
        }
    })();
    // Independently close borrowers and native owner before judging the consumer.
    endpoint.close();
    engine.close();
    drop(endpoint);
    drop(stream);
    drop(control);
    let peer_cleanup = runtime.block_on(qmp.close());
    let process_cleanup = owner.close();
    for signal in signals {
        signal_hook::low_level::unregister(signal);
    }
    assert!(!directory.exists());
    assert!(!PathBuf::from(format!("/proc/{pid}")).exists());
    let mut report = result.unwrap();
    peer_cleanup.unwrap();
    process_cleanup.unwrap();
    report["result"] = serde_json::json!("passed");
    report["cleanup_verified"] = serde_json::json!(true);
    println!("NATIVE_REPORT {report}");
}
