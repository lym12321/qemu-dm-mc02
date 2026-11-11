//! Diagnose guest tick progress using the same native owner/pump as Web Lab.
//! HMP memory reads are diagnostic observations, never board state inputs.
#[path = "../src/cli_support.rs"]
#[allow(dead_code)] // Reuse signal cleanup; the example does not use CLI error formatting.
mod cli_support;
#[path = "support/host_cpu.rs"]
mod host_cpu;
use clap::Parser;
use object::{Object, ObjectSymbol};
use serde_json::{Value, json};
use sim_host::{
    lab::{ConnectedLab, ImuMode, LabOptions, channels::ChannelData, with_lab},
    qmp::commands,
};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

#[derive(Parser)]
struct Args {
    firmware: PathBuf,
    #[arg(long)]
    qemu: Option<PathBuf>,
    #[arg(long, default_value_t = 10, value_parser = clap::value_parser!(u64).range(1..=60))]
    seconds: u64,
    #[arg(long, default_value = "xTickCount")]
    tick_symbol: String,
    #[arg(long, default_value_t = 1000, value_parser = clap::value_parser!(u32).range(1..))]
    tick_hz: u32,
    #[arg(long)]
    imu_off: bool,
    /// Sample native process/vCPU CPU time and RSS (one CPU = 100%).
    #[arg(long)]
    host_profile: bool,
    /// Compare socket overhead with only UART1 and the IMU/ADC lane connected.
    #[arg(long, requires = "host_profile")]
    minimal_io: bool,
    /// Enable QEMU's native lock/condition profiler; adds measurement overhead.
    #[arg(long, requires = "host_profile")]
    sync_profile: bool,
    /// Trace native SysTick expiry/IRQ entry for the final two seconds.
    #[arg(long, requires = "host_profile")]
    irq_trace: bool,
}

fn invalid(message: impl Into<String>) -> sim_core::Failure {
    Arc::new(sim_core::Error::State(message.into()))
}

fn monitor(lab: &mut ConnectedLab<'_>, text: &str) -> sim_core::Result<String> {
    lab.command(commands::human_monitor_command {
        command_line: text.into(),
        cpu_index: None,
    })
}

fn tick(lab: &mut ConnectedLab<'_>, address: u64) -> sim_core::Result<u32> {
    let value = lab.command(commands::human_monitor_command {
        command_line: format!("xp /1wx 0x{address:x}"),
        cpu_index: None,
    })?;
    value
        .rsplit_once("0x")
        .and_then(|(_, word)| u32::from_str_radix(word.trim(), 16).ok())
        .ok_or_else(|| invalid(format!("invalid tick memory reply: {value}")))
}

fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let args = Args::parse();
    if args.irq_trace && args.seconds < 3 {
        return Err("--irq-trace needs --seconds >= 3".into());
    }
    let mut bytes = Vec::new();
    fs::File::open(&args.firmware)?
        .take(64 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 64 * 1024 * 1024 {
        return Err("ELF exceeds 64 MiB".into());
    }
    let object = object::File::parse(bytes.as_slice())?;
    let symbol = object
        .symbols()
        .find(|symbol| symbol.name().ok() == Some(&args.tick_symbol))
        .ok_or("tick symbol not found; use an ELF with symbols")?;
    let address = symbol.address();
    if symbol.size() != 4 || address > u32::MAX.into() || address % 4 != 0 {
        return Err("tick symbol must be an aligned 32-bit counter".into());
    }
    // Keep the parsed symbol and native image from the same bytes even if the
    // user's linker replaces its output during the diagnostic run.
    let image = tempfile::NamedTempFile::new()?;
    fs::write(image.path(), &bytes)?;
    let trace_file = if args.irq_trace {
        Some(tempfile::NamedTempFile::new()?)
    } else {
        None
    };
    let mut options = LabOptions::new(image.path());
    options.qemu = args.qemu;
    if !args.minimal_io {
        options.uarts = sim_host::boards::launch::UARTS
            .iter()
            .map(|s| (*s).into())
            .collect();
        options.cans = sim_host::boards::launch::CANS
            .iter()
            .map(|s| (*s).into())
            .collect();
    }
    options.imu = if args.imu_off {
        ImuMode::Off
    } else {
        ImuMode::Stationary
    };
    options.adc = true; // Keep the native RESET observation lane enabled.
    options.timeout = Duration::from_secs(args.seconds + 20);
    let signals = cli_support::Signals::new()?;
    options.cancellation = Some(signals.cancelled.clone());
    let started = Instant::now();
    let mut samples: Vec<Value> = Vec::new();
    let mut uart_bytes = 0u64;
    let mut last_logged_ms = None;
    let mut line = Vec::new();
    let mut host_info = Value::Null;
    let mut jit_info = String::new();
    let mut uart_counts: BTreeMap<_, u64> = options.uarts.iter().map(|s| (s.clone(), 0)).collect();
    let mut can_counts: BTreeMap<_, u64> = options.cans.iter().map(|s| (s.clone(), 0)).collect();
    let mut sync_reports = Vec::new();
    let mut trace_window = Value::Null;
    let result = with_lab(options, |lab| {
        let host = if args.host_profile {
            let cpus = serde_json::to_value(lab.command(commands::query_cpus_fast {})?)
                .map_err(|e| invalid(e.to_string()))?;
            let tid = cpus[0]["thread-id"]
                .as_u64()
                .and_then(|id| u32::try_from(id).ok())
                .ok_or_else(|| invalid("missing native vCPU thread ID"))?;
            host_info = json!({"qemu_pid": lab.pid(), "vcpu_tid": tid,
                "platform": std::env::consts::OS,
                "cpu_units": "cumulative seconds; delta / host elapsed = logical CPUs used",
                "sync_profile_enabled": args.sync_profile});
            Some(
                host_cpu::Observer::new(lab.pid(), tid)
                    .map_err(|e| invalid(format!("host CPU observer: {e}")))?,
            )
        } else {
            None
        };
        if args.sync_profile {
            monitor(lab, "sync-profile on")?;
            if !monitor(lab, "sync-profile")?.contains("sync-profile is on") {
                return Err(invalid("native sync-profile did not enable"));
            }
        }
        lab.resume()?;
        let first_tick_deadline = Instant::now() + Duration::from_secs(10);
        let mut origin = None;
        let mut next_sample = Instant::now();
        loop {
            let (snapshot, events) = lab.pump()?;
            let had_logged_ms = last_logged_ms.is_some();
            for event in events {
                if args.host_profile {
                    match &event.data {
                        ChannelData::Uart(bytes) => {
                            *uart_counts.get_mut(&event.endpoint).unwrap() += bytes.len() as u64;
                        }
                        ChannelData::Can(_) => {
                            *can_counts.get_mut(&event.endpoint).unwrap() += 1;
                        }
                        _ => (),
                    }
                }
                match event.data {
                    ChannelData::Reset => return Err(invalid("native RESET during measurement")),
                    ChannelData::Uart(bytes) if event.endpoint == "uart1" => {
                        uart_bytes += bytes.len() as u64;
                        for byte in bytes {
                            if byte == b'\n' {
                                if let Some((_, suffix)) = std::str::from_utf8(&line)
                                    .ok()
                                    .and_then(|text| text.rsplit_once("ts: "))
                                {
                                    if let Some(value) = suffix
                                        .split(|c: char| !c.is_ascii_digit())
                                        .next()
                                        .and_then(|digits| digits.parse::<u32>().ok())
                                    {
                                        last_logged_ms = Some(value);
                                    }
                                }
                                line.clear();
                            } else if line.len() < 4096 {
                                line.push(byte);
                            }
                        }
                    }
                    _ => (),
                }
            }
            if args.sync_profile && !had_logged_ms && last_logged_ms.is_some() {
                sync_reports.push(json!({"phase": "before_first_ts_observed",
                    "wall_s": started.elapsed().as_secs_f64(),
                    "report": monitor(lab, "info sync-profile 20")?}));
                monitor(lab, "sync-profile reset")?;
            }
            if Instant::now() >= next_sample {
                let before = started.elapsed().as_secs_f64();
                let ticks = tick(lab, address)?;
                let after = started.elapsed().as_secs_f64();
                if ticks != 0 || origin.is_some() {
                    if let Some(previous) = samples.last() {
                        let old = previous["ticks"].as_u64().unwrap() as u32;
                        if ticks.wrapping_sub(old) >= 0x8000_0000 {
                            return Err(invalid("tick counter moved backwards"));
                        }
                    }
                    let origin = origin.get_or_insert_with(Instant::now);
                    let mut sample = json!({"wall_before_s": before, "wall_after_s": after,
                        "ticks": ticks, "qemu_ns": snapshot["timestamp_ns"],
                        "uart1_bytes": uart_bytes, "last_logged_ms": last_logged_ms});
                    if let Some(host) = &host {
                        let before = started.elapsed().as_secs_f64();
                        let mut resources = host
                            .sample()
                            .map_err(|e| invalid(format!("host CPU sample: {e}")))?
                            .json();
                        resources["wall_before_s"] = json!(before);
                        resources["wall_after_s"] = json!(started.elapsed().as_secs_f64());
                        sample["host"] = resources;
                    }
                    samples.push(sample);
                    if let Some(trace_file) = &trace_file {
                        if trace_window.is_null()
                            && origin.elapsed() >= Duration::from_secs(args.seconds - 2)
                        {
                            let path = trace_file
                                .path()
                                .to_str()
                                .ok_or_else(|| invalid("trace path is not UTF-8"))?;
                            monitor(
                                lab,
                                &format!("logfile {}", serde_json::to_string(path).unwrap()),
                            )?;
                            monitor(
                                lab,
                                "log trace:systick_timer_tick,trace:nvic_acknowledge_irq",
                            )?;
                            trace_window = json!({"wall_start_s": started.elapsed().as_secs_f64(),
                                "tick_before_enable": ticks, "sample_index_before_enable": samples.len()-1});
                        }
                    }
                    if origin.elapsed() >= Duration::from_secs(args.seconds) {
                        break;
                    }
                } else if Instant::now() >= first_tick_deadline {
                    return Err(invalid("tick stayed zero for 10 host seconds"));
                }
                next_sample = Instant::now() + Duration::from_millis(250);
            }
            lab.wait(None, Duration::from_millis(5))?;
        }
        lab.pause()?;
        if !trace_window.is_null() {
            monitor(lab, "log none")?;
            trace_window["wall_stop_s"] = json!(started.elapsed().as_secs_f64());
            trace_window["tick_after_pause"] = json!(tick(lab, address)?);
        }
        if args.sync_profile {
            monitor(lab, "sync-profile off")?;
            sync_reports.push(json!({"phase": if last_logged_ms.is_some() {
                "after_first_ts_observed" } else { "whole_run_no_ts_observed" },
                "wall_s": started.elapsed().as_secs_f64(),
                "report": monitor(lab, "info sync-profile 20")?}));
        }
        if args.host_profile {
            // Traverse native TB statistics only after the timed interval ends.
            jit_info = monitor(lab, "info jit")?;
            if !jit_info.contains("Translation buffer state:") {
                return Err(invalid(format!("invalid native JIT report: {jit_info}")));
            }
        }
        Ok(())
    });
    // The native log writer is now closed by with_lab, including on failures.
    // Read a bounded trace and close its file independently of the ELF cleanup.
    let trace_result = trace_file.as_ref().map(|file| -> std::io::Result<Value> {
        let mut trace = Vec::new();
        fs::File::open(file.path())?.take(64 * 1024 * 1024 + 1).read_to_end(&mut trace)?;
        if trace.len() > 64 * 1024 * 1024 {
            return Err(std::io::Error::other("IRQ trace exceeds 64 MiB"));
        }
        let text = std::str::from_utf8(&trace).map_err(std::io::Error::other)?;
        let (mut expiries, mut entries, mut pending_run, mut max_run) = (0u64, 0u64, 0u64, 0u64);
        for line in text.lines() {
            if line.contains("systick_timer_tick systick reload") {
                expiries += 1;
                pending_run += 1;
                max_run = max_run.max(pending_run);
            } else if line.contains("nvic_acknowledge_irq NVIC acknowledge IRQ: 15 ") {
                entries += 1;
                pending_run = 0;
            }
        }
        if expiries == 0 || entries == 0 {
            return Err(std::io::Error::other("IRQ trace missing native SysTick expiry/entry events"));
        }
        Ok(json!({"window": trace_window, "systick_expiries": expiries,
            "systick_irq_entries": entries, "max_expiries_between_irq_entries": max_run,
            "bytes": trace.len(), "note": "Native trace adds IO overhead; compare untraced run. Window boundaries are sequential host/QMP observations, not atomic with tick reads."}))
    }).transpose();
    let trace_cleanup = trace_file.map(|file| file.close()).transpose();
    let image_cleanup = image.close();
    let mut report = json!({"status": if result.is_ok() && image_cleanup.is_ok() && trace_result.is_ok() && trace_cleanup.is_ok() {"measured"} else {"failed"},
        "firmware": args.firmware, "tick_symbol": args.tick_symbol,
        "tick_address": format!("0x{address:x}"), "tick_hz": args.tick_hz,
        "imu": if args.imu_off {"off"} else {"stationary"}, "samples": samples});
    if args.host_profile {
        report["host_profile"] = host_info;
        report["io_profile"] = json!({"mode": if args.minimal_io {"minimal"} else {"all"},
            "uart_bytes": uart_counts, "can_frames": can_counts,
            "note": "Counts are observed output. Removing active ports can change firmware behavior; compare traffic and startup as well as CPU."});
        report["jit_after_pause"] = json!(jit_info);
    }
    if args.sync_profile {
        report["sync_profile"] = json!(sync_reports);
    }
    match &trace_result {
        Ok(Some(summary)) => report["irq_trace"] = summary.clone(),
        Err(error) => report["irq_trace_error"] = json!(error.to_string()),
        _ => (),
    }
    if let Err(error) = &trace_cleanup {
        report["trace_cleanup_error"] = json!(error.to_string());
    }
    if let (Some(first), Some(last)) = (samples.first(), samples.last()) {
        let wall = (last["wall_before_s"].as_f64().unwrap()
            + last["wall_after_s"].as_f64().unwrap()
            - first["wall_before_s"].as_f64().unwrap()
            - first["wall_after_s"].as_f64().unwrap())
            / 2.0;
        if wall > 0.0 {
            let ticks = (last["ticks"].as_u64().unwrap() as u32)
                .wrapping_sub(first["ticks"].as_u64().unwrap() as u32);
            report["wall_seconds"] = json!(wall);
            report["tick_delta"] = json!(ticks);
            report["tick_realtime_factor"] = json!(ticks as f64 / args.tick_hz as f64 / wall);
            report["qemu_realtime_factor"] = json!(
                (last["qemu_ns"].as_u64().unwrap() - first["qemu_ns"].as_u64().unwrap()) as f64
                    / 1e9
                    / wall
            );
        }
    }
    if let Err(error) = &result {
        report["error"] = json!(error.to_string());
        report["notes"] = json!(error.notes);
        report["cleanup_failed"] = json!(error.cleanup_failed);
    }
    if let Err(error) = &image_cleanup {
        report["input_cleanup_error"] = json!(error.to_string());
    }
    // with_lab has completed independent peer/process cleanup before publishing.
    println!("{}", serde_json::to_string_pretty(&report)?);
    result.map_err(|error| -> Box<dyn std::error::Error + Send + Sync> {
        error.to_string().into()
    })?;
    image_cleanup?;
    trace_result?;
    trace_cleanup?;
    Ok(())
}
