//! Borrowed pipe/TTY console over the same native Lab owner as other clients.
use super::{
    channels::{ChannelData, ChannelEvent},
    command::{parse, Command, Lines},
    input::{BorrowedInput, Input},
    with_lab, AdcPausedError, LabError, LabOptions,
};
use sim_core::{Error, Failure, Result};
use std::{io::Write, sync::Arc, time::Duration};

pub const HELP: &str = "Commands:\n  help | state | pause | cont | quit\n  key press|release\n  gpio A0 0|1\n  vin MILLIVOLTS\n  uart uart1 TEXT\n  can can1 ID FLAGS DLC HEX\n  adc chN raw|voltage_uv VALUE\nUART text uses UTF-8; quotes group spaces. No shell expansion or reset command.\n";

fn io(error: impl std::error::Error + Send + Sync + 'static) -> Failure {
    Arc::new(error)
}
fn write(output: &mut impl Write, text: &str) -> Result<()> {
    output.write_all(text.as_bytes()).map_err(io)?;
    output.flush().map_err(io)
}
fn events(output: &mut impl Write, events: Vec<ChannelEvent>) -> Result<()> {
    for event in events {
        let line = match event.data {
            ChannelData::Uart(bytes) => {
                let escaped: String = bytes
                    .iter()
                    .flat_map(|byte| std::ascii::escape_default(*byte))
                    .map(char::from)
                    .collect();
                format!(
                    "UART {} poll_sample_ns={} b\"{escaped}\"\n",
                    event.endpoint, event.timestamp_ns
                )
            }
            ChannelData::Can(frame) => {
                let data: String = frame
                    .data()
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect();
                format!(
                    "CAN {} native_timestamp_ns={} id=0x{:x} flags=0x{:x} dlc={} data={data}\n",
                    event.endpoint,
                    frame.timestamp_ns(),
                    frame.can_id(),
                    frame.flags(),
                    frame.dlc()
                )
            }
        };
        write(output, &line)?;
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InteractiveResult {
    pub commands: u64,
    pub observation_batches: u64,
    pub end_ns: u64,
}

/// Input remains borrowed and exclusively consumed here. Synchronous output
/// errors are terminal; a blocking caller-owned sink cannot be preempted.
pub fn run_interactive(
    options: LabOptions,
    input: BorrowedInput<'_>,
    output: &mut impl Write,
    paused: bool,
) -> std::result::Result<InteractiveResult, LabError> {
    let mut driver = Input::new(input).map_err(|error| {
        LabError::from(if error.kind() == std::io::ErrorKind::InvalidInput {
            Arc::new(Error::Admission(error.to_string())) as Failure
        } else {
            io(error)
        })
    })?;
    if paused && options.gdb_port.is_none() {
        return Err((Arc::new(Error::Admission(
            "paused Lab console requires a native GDB port".into(),
        )) as Failure)
            .into());
    }
    let (uarts, cans, adc) = (options.uarts.clone(), options.cans.clone(), options.adc);
    with_lab(options, |lab| {
        write(output, "Lab connected; enter help for commands\n")?;
        if !paused {
            lab.resume()?;
        }
        write(output, HELP)?;
        let mut lines = Lines::default();
        let mut commands = 0;
        let mut batches = 0;
        loop {
            let (_, observations) = lab.pump()?;
            batches += 1;
            events(output, observations)?;
            if lines.needs_read()
                && (driver.buffered() || lab.wait(Some(input), Duration::from_millis(5))?)
            {
                let mut bytes = [0; 4096];
                if let Some(count) = driver.read(&mut bytes, output).map_err(io)? {
                    lines.feed(&bytes[..count]);
                }
            }
            let Some(line) = lines.next_line() else {
                if lines.finished() {
                    break;
                }
                continue;
            };
            let command = match line
                .map_err(str::to_owned)
                .and_then(|line| parse(&line, &uarts, &cans, adc))
            {
                Ok(command) => command,
                Err(error) => {
                    write(output, &format!("error: {error}\n"))?;
                    continue;
                }
            };
            lab.check()?;
            match command {
                Command::Empty => continue,
                Command::Quit => {
                    commands += 1;
                    break;
                }
                Command::Help => write(output, HELP)?,
                Command::State => write(output, &(lab.snapshot()?.to_string() + "\n"))?,
                Command::Pause => lab.pause()?,
                Command::Continue => lab.resume()?,
                Command::Vin(value) => lab.set_vin(value)?,
                Command::Inject(input) => {
                    if let Err(error) = lab.inject(input) {
                        if error.downcast_ref::<AdcPausedError>().is_none() {
                            return Err(error);
                        }
                        write(output, &format!("error: {error}\n"))?;
                        continue;
                    }
                }
            }
            commands += 1;
        }
        let (snapshot, observations) = lab.pump()?;
        batches += 1;
        events(output, observations)?;
        Ok(InteractiveResult {
            commands,
            observation_batches: batches,
            end_ns: snapshot["timestamp_ns"].as_u64().unwrap(),
        })
    })
}
