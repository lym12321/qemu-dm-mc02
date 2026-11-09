//! Console grammar, with no runtime effects or shell expansion.
use super::{channels::AdcValue, LabInput};
use crate::{boards, can::CanFrame};

#[derive(Clone, Debug)]
pub enum Command {
    Empty,
    Help,
    State,
    UsbState,
    UsbAttach,
    UsbDetach,
    Pause,
    Continue,
    Quit,
    Vin(u32),
    Inject(LabInput),
}

// Narrow port of CPython v3.12.3 Lib/shlex.py read_token for
// split(comments=False, posix=True), under the adjacent PSF-LICENSE.txt.
// https://github.com/python/cpython/blob/v3.12.3/Lib/shlex.py
// Changes: string iterator; only whitespace/quote/escape modes, no IO/debug,
// source inclusion, comments, punctuation, non-POSIX or shell interpretation.
pub fn split_words(text: &str) -> Result<Vec<String>, String> {
    let mut chars = text.chars();
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut started = false;
    while let Some(ch) = chars.next() {
        match (quote, ch) {
            (Some('\''), '\'') | (Some('"'), '"') => quote = None,
            (Some('\''), _) => word.push(ch),
            (_, '\\') => {
                let next = chars.next().ok_or("command ends with an escape")?;
                if quote == Some('"') && next != '"' && next != '\\' {
                    word.push('\\');
                }
                word.push(next);
                started = true;
            }
            (Some('"'), _) => word.push(ch),
            (None, '\'' | '"') => {
                quote = Some(ch);
                started = true;
            }
            (None, ' ' | '\t' | '\r' | '\n') => {
                if started {
                    words.push(std::mem::take(&mut word));
                    started = false;
                }
            }
            _ => {
                word.push(ch);
                started = true;
            }
        }
    }
    if quote.is_some() {
        return Err("command has an unclosed quote".into());
    }
    if started {
        words.push(word);
    }
    Ok(words)
}

fn number(text: &str, name: &str, maximum: u32) -> Result<u32, String> {
    let value = text.strip_prefix('+').unwrap_or(text);
    let (digits, radix) = if let Some(value) = value.strip_prefix("0x").or(value.strip_prefix("0X"))
    {
        (value, 16)
    } else if let Some(value) = value.strip_prefix("0o").or(value.strip_prefix("0O")) {
        (value, 8)
    } else if let Some(value) = value.strip_prefix("0b").or(value.strip_prefix("0B")) {
        (value, 2)
    } else {
        if value.len() > 1
            && value.starts_with('0')
            && value.chars().any(|ch| ch != '0' && ch != '_')
        {
            return Err(format!("{name} must be an integer"));
        }
        (value, 10)
    };
    // Python int(base=0) accepts one underscore after a radix prefix, and
    // underscores between digits. Reject other placements before removing them.
    let digits = if radix != 10 {
        digits.strip_prefix('_').unwrap_or(digits)
    } else {
        digits
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return Err(format!("{name} must be an integer"));
    }
    let value = u32::from_str_radix(&digits.replace('_', ""), radix)
        .map_err(|_| format!("{name} must be an integer in range 0..{maximum}"))?;
    if value > maximum {
        return Err(format!("{name} must be in range 0..{maximum}"));
    }
    Ok(value)
}

fn hex_bytes(text: &str) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch.is_ascii_whitespace() {
            continue;
        }
        let hi = ch.to_digit(16).filter(|_| ch.is_ascii());
        let lo = chars
            .next()
            .and_then(|ch| ch.to_digit(16).filter(|_| ch.is_ascii()));
        let (Some(hi), Some(lo)) = (hi, lo) else {
            return Err("invalid CAN frame: HEX requires pairs of hex digits".into());
        };
        bytes.push((hi * 16 + lo) as u8);
    }
    Ok(bytes)
}

pub fn parse(line: &[u8], uarts: &[String], cans: &[String], adc: bool) -> Result<Command, String> {
    let text = std::str::from_utf8(line).map_err(|_| "command must be valid UTF-8")?;
    let args = split_words(text)?;
    let args: Vec<_> = args.iter().map(String::as_str).collect();
    Ok(match args.as_slice() {
        [] => Command::Empty,
        ["help"] => Command::Help,
        ["state"] => Command::State,
        ["usb", "state"] => Command::UsbState,
        ["usb", "attach"] => Command::UsbAttach,
        ["usb", "detach"] => Command::UsbDetach,
        ["pause"] => Command::Pause,
        ["cont"] => Command::Continue,
        ["quit"] => Command::Quit,
        ["key", value @ ("press" | "release")] => Command::Inject(LabInput::Key(*value == "press")),
        ["gpio", pin, level] => {
            boards::gpio_pin(pin).map_err(|error| error.to_string())?;
            Command::Inject(LabInput::Gpio {
                pin: (*pin).into(),
                level: number(level, "GPIO level", 1)? != 0,
            })
        }
        ["vin", value] => Command::Vin(number(value, "VIN", u32::MAX)?),
        ["uart", port, rest @ ..] if !rest.is_empty() => {
            if !uarts.iter().any(|name| name == port) {
                return Err("UART endpoint is not configured".into());
            }
            let data = rest.join(" ").into_bytes();
            if data.len() > 4096 {
                return Err("UART text must be at most 4096 bytes".into());
            }
            Command::Inject(LabInput::Uart {
                endpoint: (*port).into(),
                data,
            })
        }
        ["can", port, id, flags, dlc, data] => {
            if !cans.iter().any(|name| name == port) {
                return Err("CAN endpoint is not configured".into());
            }
            let frame = CanFrame::new(
                number(id, "CAN ID", 0x1fff_ffff)?,
                number(flags, "CAN flags", 15)?,
                Some(number(dlc, "CAN DLC", 15)? as u8),
                hex_bytes(data)?,
                0,
            )
            .map_err(|error| format!("invalid CAN frame: {error}"))?;
            Command::Inject(LabInput::Can {
                endpoint: (*port).into(),
                frame,
            })
        }
        ["adc", channel, kind @ ("raw" | "voltage_uv"), value] => {
            if !adc {
                return Err("ADC co-sim is not configured".into());
            }
            let channel = channel
                .strip_prefix("ch")
                .and_then(|text| {
                    text.parse::<u16>()
                        .ok()
                        .filter(|channel| *channel <= 31 && text == channel.to_string())
                })
                .ok_or("ADC1 channel must be ch0 through ch31")?;
            let value = if *kind == "raw" {
                AdcValue::Raw(number(value, "ADC raw", 65535)? as u16)
            } else {
                AdcValue::Voltage(number(value, "ADC voltage_uv", 3_300_000)?)
            };
            Command::Inject(LabInput::Adc { channel, value })
        }
        _ => return Err("invalid command; use help for commands and arguments".into()),
    })
}

/// Bounded stream assembly; an overlong line is rejected once and discarded
/// through its newline. At most one result is consumed per console pump.
#[derive(Default)]
pub struct Lines {
    bytes: Vec<u8>,
    discarding: bool,
    eof: bool,
}
impl Lines {
    pub fn needs_read(&self) -> bool {
        !self.eof && !self.bytes.contains(&b'\n')
    }
    pub fn finished(&self) -> bool {
        self.eof && self.bytes.is_empty()
    }
    pub fn feed(&mut self, bytes: &[u8]) {
        if bytes.is_empty() {
            self.eof = true;
            return;
        }
        if self.discarding {
            let Some(index) = bytes.iter().position(|byte| *byte == b'\n') else {
                return;
            };
            self.discarding = false;
            self.bytes.extend_from_slice(&bytes[index + 1..]);
        } else {
            self.bytes.extend_from_slice(bytes);
        }
    }
    pub fn next_line(&mut self) -> Option<Result<Vec<u8>, &'static str>> {
        let length = self.bytes.iter().position(|byte| *byte == b'\n');
        if let Some(length) = length {
            let mut rest = self.bytes.split_off(length + 1);
            std::mem::swap(&mut rest, &mut self.bytes);
            rest.truncate(length);
            return Some(if length <= 4096 {
                Ok(rest)
            } else {
                Err("command line exceeds 4096 bytes")
            });
        }
        if self.bytes.len() > 4096 {
            self.bytes.clear();
            self.discarding = !self.eof;
            return Some(Err("command line exceeds 4096 bytes"));
        }
        if self.eof && !self.bytes.is_empty() {
            return Some(Ok(std::mem::take(&mut self.bytes)));
        }
        None
    }
}
