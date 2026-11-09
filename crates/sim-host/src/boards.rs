//! Schema-1 public board observations, ported from sim.boards.qemu. Values
//! remain native JSON; GPIO projection reads only the public sampled helper.
pub use crate::board_launch as launch;
use sim_core::{Error, Failure};
use std::sync::Arc;
fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}
use serde_json::Value;
use sim_core::Result;

pub fn gpio_pin(pin: &str) -> Result<(&str, u32)> {
    let bytes = pin.as_bytes();
    if bytes.len() < 2 || !matches!(bytes[0], b'A'..=b'H') {
        return Err(admission("GPIO pin must be A0 through H15"));
    }
    let number = &pin[1..];
    let index = number
        .parse::<u32>()
        .ok()
        .filter(|index| *index < 16 && index.to_string() == number)
        .ok_or_else(|| admission("GPIO pin must be A0 through H15"))?;
    Ok((&pin[..1], index))
}
pub(crate) fn uint(value: &Value, maximum: u64, name: &str) -> Result<u64> {
    value
        .as_u64()
        .filter(|value| *value <= maximum)
        .ok_or_else(|| state_error(format!("invalid {name}")))
}
pub fn validate_snapshot(state: &Value) -> Result<()> {
    if state["schema"] != 1 {
        return Err(state_error("unsupported board-state schema"));
    }
    uint(&state["timestamp_ns"], u64::MAX, "board timestamp")?;
    uint(&state["vin_mv"], u32::MAX.into(), "board vin_mv")?;
    if state["board"].as_str().is_none_or(str::is_empty) {
        return Err(state_error("board-state board must be nonempty"));
    }
    let gpio = state["gpio"]
        .as_object()
        .ok_or_else(|| state_error("board GPIO must be object"))?;
    if gpio.len() != 8 {
        return Err(state_error("board GPIO needs exactly banks A through H"));
    }
    for bank in 'A'..='H' {
        let bank = gpio
            .get(&bank.to_string())
            .ok_or_else(|| state_error("missing GPIO bank"))?;
        for (field, max) in [
            ("moder", u32::MAX as u64),
            ("sampled", 0xffff),
            ("odr", 0xffff),
            ("output_mask", 0xffff),
        ] {
            uint(&bank[field], max, field)?;
        }
    }
    for alias in ["led", "key"] {
        gpio_pin(
            state[alias]["pin"]
                .as_str()
                .ok_or_else(|| state_error("missing board alias pin"))?,
        )?;
    }
    uint(&state["led"]["rgb"], 0xff_ffff, "led.rgb")?;
    uint(&state["led"]["brightness"], 0xff, "led.brightness")?;
    if !matches!(
        state["led"]["source"].as_str(),
        Some("ws2812-dma" | "gpio-latch")
    ) {
        return Err(state_error("unsupported led.source"));
    }
    if !state["key"]["pressed"].is_boolean() {
        return Err(state_error("key.pressed must be boolean"));
    }
    uint(&state["key"]["level"], 1, "key.level")?;
    Ok(())
}
pub fn digital_level(endpoint: &str, state: &Value) -> Result<bool> {
    validate_snapshot(state)?;
    match endpoint {
        "key" => Ok(state["key"]["level"] == 1),
        "led" => Ok(state["led"]["rgb"].as_u64().unwrap() != 0),
        _ => {
            let pin = endpoint
                .strip_prefix("gpio.")
                .ok_or_else(|| admission("digital endpoint must be gpio.PIN, key or led"))?;
            let (bank, index) = gpio_pin(pin)?;
            Ok(state["gpio"][bank]["sampled"].as_u64().unwrap() & (1 << index) != 0)
        }
    }
}
