//! Pure assertion admission shared by scene execution and trace replay.

use super::yaml::{fields, parse_time, text_bytes, ManifestError, Mapping, Value};
use crate::assertions::{Assertion, CanMatch, Condition};

type Result<T> = std::result::Result<T, ManifestError>;

pub(crate) fn member<'a>(mapping: &'a Mapping, key: &str) -> Option<&'a Value> {
    mapping
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

pub(crate) fn required<'a>(mapping: &'a Mapping, key: &str) -> &'a Value {
    member(mapping, key).expect("required field was admitted")
}

pub(crate) fn unsigned(value: &Value, maximum: u64, label: &str) -> Result<u64> {
    value
        .as_integer()
        .and_then(|number| u64::try_from(number).ok())
        .filter(|number| *number <= maximum)
        .ok_or_else(|| ManifestError::new(format!("{label} must be an integer in 0..{maximum}")))
}

pub(crate) fn string<'a>(value: &'a Value, label: &str) -> Result<&'a str> {
    value
        .as_str()
        .ok_or_else(|| ManifestError::new(format!("{label} must be a string")))
}

pub(crate) fn data_hex(value: &Value, label: &str) -> Result<Vec<u8>> {
    let value = string(value, label)?;
    if value.len() > 128
        || value.len() % 2 != 0
        || !value.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err(ManifestError::new(format!(
            "{label} requires at most 64 contiguous hex pairs"
        )));
    }
    Ok(value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect())
}

pub fn parse_assertion(
    value: &Value,
    index: usize,
    mut endpoint_admit: impl FnMut(&str, &str) -> Result<String>,
) -> Result<Assertion> {
    let label = format!("assertions[{index}]");
    let mapping = fields(
        value,
        &["before", "uart_contains", "digital_equals", "can_seen"],
        &["before"],
        &label,
    )?;
    let selected: Vec<_> = ["uart_contains", "digital_equals", "can_seen"]
        .into_iter()
        .filter(|name| member(mapping, name).is_some())
        .collect();
    if selected.len() != 1 {
        return Err(ManifestError::new(format!(
            "{label} requires exactly one assertion kind"
        )));
    }
    let kind = selected[0];
    let before_ns = parse_time(
        required(mapping, "before"),
        &format!("{label}.before"),
        false,
    )?;
    let (allowed, required_fields): (&[&str], &[&str]) = match kind {
        "uart_contains" => (&["endpoint", "text"], &["endpoint", "text"]),
        "digital_equals" => (&["endpoint", "value"], &["endpoint", "value"]),
        _ => (
            &["endpoint", "id", "flags", "dlc", "data_hex"],
            &["endpoint", "id"],
        ),
    };
    let condition = fields(
        required(mapping, kind),
        allowed,
        required_fields,
        &format!("{label}.{kind}"),
    )?;
    let endpoint = endpoint_admit(
        kind,
        string(
            required(condition, "endpoint"),
            &format!("{label}.endpoint"),
        )?,
    )?;
    let condition = match kind {
        "uart_contains" => Condition::UartContains(text_bytes(
            required(condition, "text"),
            &format!("{label}.text"),
            false,
            4096,
        )?),
        "digital_equals" => Condition::DigitalEquals(
            unsigned(required(condition, "value"), 1, &format!("{label}.value"))? == 1,
        ),
        _ => Condition::CanSeen(CanMatch {
            can_id: unsigned(
                required(condition, "id"),
                0x1fff_ffff,
                &format!("{label}.id"),
            )? as u32,
            flags: member(condition, "flags")
                .map(|v| unsigned(v, 15, &format!("{label}.flags")).map(|v| v as u32))
                .transpose()?,
            dlc: member(condition, "dlc")
                .map(|v| unsigned(v, 15, &format!("{label}.dlc")).map(|v| v as u8))
                .transpose()?,
            data: member(condition, "data_hex")
                .map(|v| data_hex(v, &format!("{label}.data_hex")))
                .transpose()?,
        }),
    };
    let assertion = Assertion {
        before_ns,
        endpoint,
        condition,
    };
    assertion
        .validate()
        .map_err(|error| ManifestError::new(format!("{label}: {error}")))?;
    Ok(assertion)
}
