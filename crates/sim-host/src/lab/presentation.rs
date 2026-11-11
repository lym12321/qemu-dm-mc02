//! JSON and pin projections for browser presentation, outside the native command API.
use super::{
    BoardCommand, LabInput,
    channels::{AdcValue, ChannelData, ChannelEvent},
};
use crate::{boards, can::CanFrame};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
type Result<T> = std::result::Result<T, String>;
fn refused(message: impl Into<String>) -> String {
    message.into()
}
fn fields<'a>(value: &'a Value, expected: &[&str]) -> Result<&'a serde_json::Map<String, Value>> {
    value
        .as_object()
        .filter(|map| {
            map.len() == expected.len() && expected.iter().all(|key| map.contains_key(*key))
        })
        .ok_or_else(|| refused(format!("expected fields: {}", expected.join(", "))))
}
fn uint(value: &Value, name: &str, maximum: u64) -> Result<u64> {
    value
        .as_u64()
        .filter(|value| *value <= maximum)
        .ok_or_else(|| refused(format!("{name} must be an integer in 0..{maximum}")))
}
fn text<'a>(value: &'a Value, label: &str) -> Result<&'a str> {
    value
        .as_str()
        .ok_or_else(|| refused(format!("{label} must be a string")))
}
pub(super) fn decode(action: &str, args: &Value) -> Result<BoardCommand> {
    Ok(match action {
        "run" | "pause" => {
            fields(args, &[])?;
            if action == "run" {
                BoardCommand::Resume
            } else {
                BoardCommand::Pause
            }
        }
        "usb_attach" | "usb_detach" => {
            fields(args, &[])?;
            if action == "usb_attach" {
                BoardCommand::UsbAttach
            } else {
                BoardCommand::UsbDetach
            }
        }
        "key" => {
            fields(args, &["pressed"])?;
            BoardCommand::Input(LabInput::Key(
                args["pressed"]
                    .as_bool()
                    .ok_or_else(|| refused("pressed must be a boolean"))?,
            ))
        }
        "gpio" => {
            fields(args, &["pin", "level"])?;
            let pin = text(&args["pin"], "pin")?;
            boards::gpio_pin(pin).map_err(|e| refused(e.to_string()))?;
            BoardCommand::Input(LabInput::Gpio {
                pin: pin.into(),
                level: uint(&args["level"], "level", 1)? != 0,
            })
        }
        "vin" => {
            fields(args, &["millivolts"])?;
            BoardCommand::Vin(uint(&args["millivolts"], "millivolts", u32::MAX.into())? as u32)
        }
        "uart" => {
            fields(args, &["endpoint", "data_b64"])?;
            let endpoint = text(&args["endpoint"], "endpoint")?;
            if !boards::launch::UARTS.contains(&endpoint) {
                return Err(refused("unknown UART endpoint"));
            }
            let encoded = text(&args["data_b64"], "data_b64")?;
            if encoded.len() > 5464 {
                return Err(refused("UART input must be at most 4096 bytes"));
            }
            let data = STANDARD
                .decode(encoded)
                .map_err(|e| refused(e.to_string()))?;
            if data.len() > 4096 {
                return Err(refused("UART input must be at most 4096 bytes"));
            }
            BoardCommand::Input(LabInput::Uart {
                endpoint: endpoint.into(),
                data,
            })
        }
        "can" => {
            fields(args, &["endpoint", "can_id", "flags", "dlc", "data_hex"])?;
            let endpoint = text(&args["endpoint"], "endpoint")?;
            if !boards::launch::CANS.contains(&endpoint) {
                return Err(refused("unknown CAN endpoint"));
            }
            let encoded = text(&args["data_hex"], "data_hex")?;
            if encoded.len() > 192 {
                return Err(refused("CAN payload must be at most 64 bytes"));
            }
            let data = super::command::decode_uart("hex", "none", encoded)?;
            let frame = CanFrame::new(
                uint(&args["can_id"], "can_id", 0x1fffffff)? as u32,
                uint(&args["flags"], "flags", 15)? as u32,
                Some(uint(&args["dlc"], "dlc", 15)? as u8),
                data,
                0,
            )
            .map_err(|e| refused(e.to_string()))?;
            BoardCommand::Input(LabInput::Can {
                endpoint: endpoint.into(),
                frame,
            })
        }
        "adc" => {
            let map = args
                .as_object()
                .ok_or_else(|| refused("ADC args must be an object"))?;
            let (field, maximum) = if map.contains_key("raw") {
                ("raw", 65535)
            } else {
                ("voltage_uv", 3300000)
            };
            fields(args, &["channel", field])?;
            let channel = uint(&args["channel"], "channel", 31)? as u16;
            let value = uint(&args[field], field, maximum)?;
            BoardCommand::Input(LabInput::Adc {
                channel,
                value: if field == "raw" {
                    AdcValue::Raw(value as u16)
                } else {
                    AdcValue::Voltage(value as u32)
                },
            })
        }
        _ => return Err(refused("unknown action")),
    })
}

pub(super) fn project(mut snapshot: Value) -> sim_core::Result<(Value, Value)> {
    let mut pins = Vec::with_capacity(128);
    for port in b'A'..=b'H' {
        let bank = &snapshot["gpio"][char::from(port).to_string()];
        for bit in 0..16 {
            let pin = format!("{}{bit}", char::from(port));
            pins.push(
                json!({"pin":pin,"mode":(bank["moder"].as_u64().unwrap() >> (bit*2)) & 3,
                "level":u8::from(boards::digital_level(&format!("gpio.{pin}"), &snapshot)?),
                "odr":(bank["odr"].as_u64().unwrap() >> bit) & 1,
                "output":bank["output_mask"].as_u64().unwrap() & (1 << bit) != 0}),
            );
        }
    }
    snapshot["timestamp_ns"] = snapshot["timestamp_ns"]
        .as_u64()
        .unwrap()
        .to_string()
        .into();
    Ok((snapshot, pins.into()))
}
pub(super) fn event(event: ChannelEvent) -> Value {
    match event.data {
        ChannelData::Uart(data) => json!({"type":"uart","endpoint":event.endpoint,
            "timestamp_ns":event.timestamp_ns.to_string(),"data_b64":STANDARD.encode(data)}),
        ChannelData::Can(frame) => json!({"type":"can","endpoint":event.endpoint,
            "timestamp_ns":event.timestamp_ns.to_string(),"can_id":frame.can_id(),
            "flags":frame.flags(),"dlc":frame.dlc(),
            "data_hex":frame.data().iter().map(|byte| format!("{byte:02x}")).collect::<String>()}),
        ChannelData::Reset => json!({"type":"reset","endpoint":event.endpoint,
            "timestamp_ns":event.timestamp_ns.to_string()}),
    }
}
