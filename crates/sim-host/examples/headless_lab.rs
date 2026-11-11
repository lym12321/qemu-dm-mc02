//! `cargo run -p sim-host --features lab --example headless_lab -- ELF [QEMU]`
use sim_host::lab::{channels::ChannelData, with_lab, BoardCommand, LabOptions};
use std::{path::PathBuf, time::Duration};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let firmware = args.next().ok_or("usage: headless_lab ELF [QEMU]")?;
    let mut options = LabOptions::new(firmware);
    options.qemu = args.next().map(PathBuf::from);
    if args.next().is_some() {
        return Err("usage: headless_lab ELF [QEMU]".into());
    }
    let (snapshot, received) = with_lab(options, |lab| {
        lab.apply(BoardCommand::Resume)?;
        let mut received = 0usize;
        loop {
            let (snapshot, events) = lab.pump()?;
            for event in events {
                if let ChannelData::Uart(bytes) = event.data {
                    received += bytes.len();
                }
            }
            if snapshot["timestamp_ns"].as_u64().unwrap() >= 1_000_000_000 {
                lab.apply(BoardCommand::Pause)?;
                return Ok((lab.snapshot()?, received));
            }
            lab.wait(None, Duration::from_millis(5))?;
        }
    })?;
    // The result is published after native process and peer cleanup.
    println!("UART bytes: {received}\n{snapshot}");
    Ok(())
}
