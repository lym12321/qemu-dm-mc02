//! Borrowed pipe/TTY console over the same native Lab owner as other clients.
use super::{
    AdcPausedError, BoardCommand, LabError, LabOptions, StartupMode,
    channels::{ChannelData, ChannelEvent},
    command::{Command, Lines, UartReceiveFormat, parse},
    input::{BorrowedInput, Input},
    with_lab,
};
use sim_core::{Error, Failure, Result};
use std::{collections::BTreeMap, io::Write, sync::Arc, time::Duration};

pub const HELP: &str = "Commands:\n  help | state | pause | cont | quit\n  usb state|attach|detach\n  key press|release\n  gpio A0 0|1\n  vin MILLIVOLTS\n  uart uart1 TEXT\n  uart-send uart1 text|hex none|lf|crlf|cr PAYLOAD\n  receive-format escaped|text|hex\n  can can1 ID FLAGS DLC HEX\n  adc chN raw|voltage_uv VALUE\n  actions | action NAME\n";

fn io(error: impl std::error::Error + Send + Sync + 'static) -> Failure {
    Arc::new(error)
}
fn write(output: &mut impl Write, text: &str) -> Result<()> {
    output.write_all(text.as_bytes()).map_err(io)?;
    output.flush().map_err(io)
}
fn events(
    output: &mut impl Write,
    events: Vec<ChannelEvent>,
    format: UartReceiveFormat,
) -> Result<()> {
    for event in events {
        let line = match event.data {
            ChannelData::Uart(bytes) => {
                if format == UartReceiveFormat::Text {
                    output.write_all(&bytes).map_err(io)?;
                    output.flush().map_err(io)?;
                    continue;
                }
                if format == UartReceiveFormat::Hex {
                    let data: String = bytes.iter().map(|byte| format!("{byte:02x} ")).collect();
                    write(output, &format!("UART {} {data}\n", event.endpoint))?;
                    continue;
                }
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
            ChannelData::Reset => format!(
                "RESET {} native_timestamp_ns={} input lane reinitialized\n",
                event.endpoint, event.timestamp_ns
            ),
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
    run_interactive_with_project(
        options,
        input,
        output,
        if paused {
            StartupMode::WaitGdb
        } else {
            StartupMode::Run
        },
        Duration::from_secs(1),
        &BTreeMap::new(),
    )
}

pub fn run_interactive_with_project(
    options: LabOptions,
    input: BorrowedInput<'_>,
    output: &mut impl Write,
    startup: StartupMode,
    user_hold: Duration,
    actions: &BTreeMap<String, BoardCommand>,
) -> std::result::Result<InteractiveResult, LabError> {
    let mut driver = Input::new(input).map_err(|error| {
        LabError::from(if error.kind() == std::io::ErrorKind::InvalidInput {
            Arc::new(Error::Admission(error.to_string())) as Failure
        } else {
            io(error)
        })
    })?;
    if startup == StartupMode::WaitGdb && options.gdb_port.is_none() {
        return Err((Arc::new(Error::Admission(
            "paused Lab console requires a native GDB port".into(),
        )) as Failure)
            .into());
    }
    let (uarts, cans, adc) = (options.uarts.clone(), options.cans.clone(), options.adc);
    with_lab(options, |lab| {
        write(output, "Lab connected; enter help for commands\n")?;
        lab.start(startup, user_hold)?;
        write(output, HELP)?;
        write(output, &format!("USB {}\n", lab.usb_status()))?;
        let mut lines = Lines::default();
        let mut commands = 0;
        let mut batches = 0;
        let mut receive_format = UartReceiveFormat::default();
        loop {
            let (_, observations) = lab.pump()?;
            batches += 1;
            events(output, observations, receive_format)?;
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
            let board_command = match &command {
                Command::Action(name) => match actions.get(name) {
                    Some(command) => Some(command.clone()),
                    None => {
                        write(output, &format!("error: unknown action {name}\n"))?;
                        continue;
                    }
                },
                _ => command.board_command(),
            };
            if let Some(board_command) = board_command {
                let usb = matches!(
                    board_command,
                    BoardCommand::UsbAttach | BoardCommand::UsbDetach
                );
                if let Err(error) = lab.apply(board_command) {
                    if error.downcast_ref::<AdcPausedError>().is_none()
                        && !(usb
                            && matches!(error.downcast_ref::<Error>(), Some(Error::Admission(_))))
                    {
                        return Err(error);
                    }
                    write(output, &format!("error: {error}\n"))?;
                    continue;
                }
                commands += 1;
                continue;
            }
            match command {
                Command::Empty => continue,
                Command::Quit => {
                    commands += 1;
                    break;
                }
                Command::Help => write(output, HELP)?,
                Command::State => write(output, &(lab.snapshot()?.to_string() + "\n"))?,
                Command::UsbState => write(output, &(lab.usb_status().to_string() + "\n"))?,
                Command::ReceiveFormat(format) => receive_format = format,
                Command::Actions => {
                    for name in actions.keys() {
                        write(output, &format!("{name}\n"))?;
                    }
                }
                // Native commands are dispatched through apply above.
                _ => unreachable!("native command already dispatched"),
            }
            commands += 1;
        }
        let (snapshot, observations) = lab.pump()?;
        batches += 1;
        events(output, observations, receive_format)?;
        Ok(InteractiveResult {
            commands,
            observation_batches: batches,
            end_ns: snapshot["timestamp_ns"].as_u64().unwrap(),
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_receive_preserves_fragmented_utf8_and_binary_without_chunk_separators() {
        let mut output = Vec::new();
        for bytes in [vec![0xe4], vec![0xb8, 0xad, 0, 0xff]] {
            events(
                &mut output,
                vec![ChannelEvent {
                    endpoint: "uart1".into(),
                    timestamp_ns: 7,
                    data: ChannelData::Uart(bytes),
                }],
                UartReceiveFormat::Text,
            )
            .unwrap();
        }
        assert_eq!(output, b"\xe4\xb8\xad\0\xff");
    }

    #[test]
    fn reset_remains_visible_in_raw_uart_text_mode() {
        let mut output = Vec::new();
        events(
            &mut output,
            vec![ChannelEvent {
                endpoint: "cosim".into(),
                timestamp_ns: 123,
                data: ChannelData::Reset,
            }],
            UartReceiveFormat::Text,
        )
        .unwrap();
        assert_eq!(
            output,
            b"RESET cosim native_timestamp_ns=123 input lane reinitialized\n"
        );
    }
}
