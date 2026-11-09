//! Bounded YAML syntax from yaml-rust2 with the existing PyYAML scalar policy.
//!
//! The scalar resolver expressions and bool/int/float/null/binary constructor
//! ordering below are a narrow Rust port of PyYAML 6.0.3:
//! https://github.com/yaml/pyyaml/tree/6.0.3/lib/yaml
//! Canonical sdist SHA256:
//! d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f
//! Changes: owned Rust values, bounded event composition, string-only unique
//! mappings, and explicit rejection of timestamp/set/omap/pairs objects that
//! have no supported sim/v1 schema type. YAML syntax remains owned by yaml-rust2.
//!
//! Copyright (c) 2017-2021 Ingy döt Net
//! Copyright (c) 2006-2016 Kirill Simonov
//!
//! Permission is hereby granted, free of charge, to any person obtaining a copy
//! of this software and associated documentation files (the "Software"), to deal
//! in the Software without restriction, including without limitation the rights
//! to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//! copies of the Software, and to permit persons to whom the Software is
//! furnished to do so, subject to the following conditions:
//!
//! The above copyright notice and this permission notice shall be included in
//! all copies or substantial portions of the Software.
//!
//! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//! IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//! FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//! AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//! LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//! OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
//! THE SOFTWARE.

use std::{
    borrow::Cow,
    collections::HashSet,
    fmt,
    fs::File,
    io::Read,
    path::{Path, PathBuf},
    str::Chars,
    sync::LazyLock,
};

use base64::{
    alphabet,
    engine::{general_purpose::GeneralPurpose, GeneralPurposeConfig},
    Engine,
};
use num_bigint::BigInt;
use regex::Regex;
use yaml_rust2::{
    parser::{Event, Parser, Tag},
    scanner::TScalarStyle,
};

pub const MAX_FILE_BYTES: usize = 1 << 20;
pub const MAX_TIME_NS: u64 = 3600 * 1_000_000_000;
const MAX_NODES: usize = 40_000;
const MAX_DEPTH: usize = 16;

/// Mapping order is observable in component construction and equal-time inputs.
pub type Mapping = Vec<(String, Value)>;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Integer(BigInt),
    Float(f64),
    String(String),
    Binary(Vec<u8>),
    Sequence(Vec<Value>),
    Mapping(Mapping),
}

impl Value {
    pub fn as_mapping(&self) -> Option<&Mapping> {
        match self {
            Self::Mapping(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_sequence(&self) -> Option<&[Value]> {
        match self {
            Self::Sequence(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }

    pub fn as_integer(&self) -> Option<&BigInt> {
        match self {
            Self::Integer(value) => Some(value),
            _ => None,
        }
    }

    /// Exact integer conversion; booleans and floating point values are excluded.
    pub fn as_u64(&self) -> Option<u64> {
        self.as_integer()?.try_into().ok()
    }

    /// Read an actual floating point scalar, without coercing strings or integers.
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Self::Float(value) => Some(*value),
            _ => None,
        }
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.as_mapping()?
            .iter()
            .find_map(|(name, value)| (name == key).then_some(value))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ManifestError {
    message: String,
}

impl ManifestError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for ManifestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ManifestError {}

pub type Result<T> = std::result::Result<T, ManifestError>;

/// Resolve and read one bounded UTF-8 regular file, without backend acquisition.
pub fn load_document(path: impl AsRef<Path>) -> Result<(PathBuf, Value)> {
    let source = path
        .as_ref()
        .canonicalize()
        .map_err(|error| ManifestError::new(format!("manifest cannot be opened: {error}")))?;
    if !source.is_file() {
        return Err(ManifestError::new("manifest must be a regular file"));
    }
    let input = File::open(&source)
        .map_err(|error| ManifestError::new(format!("manifest cannot be opened: {error}")))?;
    let mut raw = Vec::new();
    input
        .take((MAX_FILE_BYTES + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|error| ManifestError::new(format!("manifest cannot be read: {error}")))?;
    let document = parse_document(&raw)?;
    Ok((source, document))
}

/// Parse zero or one YAML document. Empty input is YAML null.
pub fn parse_document(raw: &[u8]) -> Result<Value> {
    if raw.len() > MAX_FILE_BYTES {
        return Err(ManifestError::new("manifest exceeds 1 MiB"));
    }
    let input = std::str::from_utf8(raw)
        .map_err(|error| ManifestError::new(format!("manifest must be valid UTF-8: {error}")))?;
    // PyYAML Reader's printable range. In particular, do not let raw NUL act
    // as yaml-rust2's end-of-input sentinel and hide a physical suffix.
    if input.chars().any(|character| {
        !matches!(character, '\t' | '\n' | '\r' | '\u{20}'..='\u{7e}' | '\u{85}'
            | '\u{a0}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}')
    }) {
        return Err(ManifestError::new(
            "YAML contains a non-printable character",
        ));
    }
    let mut composer = Composer {
        parser: Parser::new_from_str(input),
        nodes: 0,
    };
    composer.expect(Event::StreamStart)?;
    match composer.next()? {
        Event::StreamEnd => return Ok(Value::Null),
        Event::DocumentStart => {}
        _ => return Err(ManifestError::new("expected a YAML document")),
    }
    let event = composer.next()?;
    let value = composer.compose(event, 1)?;
    composer.expect(Event::DocumentEnd)?;
    if composer.next()? != Event::StreamEnd {
        return Err(ManifestError::new("expected a single YAML document"));
    }
    Ok(value)
}

struct Composer<'a> {
    parser: Parser<Chars<'a>>,
    nodes: usize,
}

impl Composer<'_> {
    fn next(&mut self) -> Result<Event> {
        self.parser
            .next_token()
            .map(|(event, _)| event)
            .map_err(|error| ManifestError::new(format!("cannot parse manifest: {error}")))
    }

    fn expect(&mut self, expected: Event) -> Result<()> {
        if self.next()? != expected {
            return Err(ManifestError::new("unexpected YAML document boundary"));
        }
        Ok(())
    }

    fn compose(&mut self, event: Event, depth: usize) -> Result<Value> {
        let anchor = match &event {
            Event::Scalar(_, _, anchor, _)
            | Event::SequenceStart(anchor, _)
            | Event::MappingStart(anchor, _) => *anchor,
            Event::Alias(_) => {
                return Err(ManifestError::new(
                    "YAML anchors and aliases are not supported",
                ));
            }
            _ => return Err(ManifestError::new("expected a YAML value")),
        };
        if anchor != 0 {
            return Err(ManifestError::new(
                "YAML anchors and aliases are not supported",
            ));
        }
        self.nodes += 1;
        if self.nodes > MAX_NODES {
            return Err(ManifestError::new("YAML node limit exceeded"));
        }
        if depth > MAX_DEPTH {
            return Err(ManifestError::new("YAML nesting limit exceeded"));
        }
        match event {
            Event::Scalar(text, style, _, tag) => scalar(text, style, tag.as_ref()),
            Event::SequenceStart(_, tag) => {
                collection_tag(tag.as_ref(), "seq")?;
                let mut values = Vec::new();
                loop {
                    let event = self.next()?;
                    if event == Event::SequenceEnd {
                        return Ok(Value::Sequence(values));
                    }
                    values.push(self.compose(event, depth + 1)?);
                }
            }
            Event::MappingStart(_, tag) => {
                collection_tag(tag.as_ref(), "map")?;
                let mut values = Vec::new();
                let mut keys = HashSet::new();
                loop {
                    let event = self.next()?;
                    if event == Event::MappingEnd {
                        return Ok(Value::Mapping(values));
                    }
                    let key = self.compose(event, depth + 1)?;
                    let Value::String(key) = key else {
                        return Err(ManifestError::new("YAML mapping keys must be strings"));
                    };
                    if !keys.insert(key.clone()) {
                        return Err(ManifestError::new(format!("duplicate YAML key: {key}")));
                    }
                    let event = self.next()?;
                    let value = self.compose(event, depth + 1)?;
                    values.push((key, value));
                }
            }
            _ => unreachable!("node event admitted above"),
        }
    }
}

fn tag_name(tag: &Tag) -> String {
    format!("{}{}", tag.handle, tag.suffix)
}

fn non_specific(tag: &Tag) -> bool {
    tag.handle.is_empty() && tag.suffix == "!"
}

fn collection_tag(tag: Option<&Tag>, expected: &str) -> Result<()> {
    if tag.is_none_or(|tag| {
        non_specific(tag) || tag_name(tag) == format!("tag:yaml.org,2002:{expected}")
    }) {
        Ok(())
    } else {
        Err(ManifestError::new("unsupported YAML collection tag"))
    }
}

// These patterns retain PyYAML's order and Python '$' final-newline behavior.
static BOOL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\A(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)(?:\n)?\z").unwrap()
});
static FLOAT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?x)\A(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))(?:\n)?\z").unwrap()
});
static INT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?x)\A(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)(?:\n)?\z").unwrap()
});
static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?x)\A(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[\x20\t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[\x20\t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)(?:\n)?\z").unwrap()
});

fn implicit_tag(text: &str) -> &'static str {
    if BOOL.is_match(text) {
        "bool"
    } else if FLOAT.is_match(text) {
        "float"
    } else if INT.is_match(text) {
        "int"
    } else if matches!(text.strip_suffix('\n').unwrap_or(text), "<<" | "=") {
        "unsupported"
    } else if matches!(
        text.strip_suffix('\n').unwrap_or(text),
        "" | "~" | "null" | "Null" | "NULL"
    ) {
        "null"
    } else if TIMESTAMP.is_match(text) {
        "timestamp"
    } else {
        "str"
    }
}

fn scalar(text: String, style: TScalarStyle, tag: Option<&Tag>) -> Result<Value> {
    let name = match tag {
        Some(tag) if !non_specific(tag) => tag_name(tag),
        _ if tag.is_some() || style == TScalarStyle::Plain => {
            format!("tag:yaml.org,2002:{}", implicit_tag(&text))
        }
        _ => return Ok(Value::String(text)),
    };
    match name.as_str() {
        "tag:yaml.org,2002:str" => Ok(Value::String(text)),
        "tag:yaml.org,2002:null" => Ok(Value::Null),
        "tag:yaml.org,2002:bool" => match text.to_lowercase().as_str() {
            "yes" | "true" | "on" => Ok(Value::Bool(true)),
            "no" | "false" | "off" => Ok(Value::Bool(false)),
            _ => Err(ManifestError::new("invalid YAML boolean")),
        },
        "tag:yaml.org,2002:int" => integer(&text).map(Value::Integer),
        "tag:yaml.org,2002:float" => float(&text).map(Value::Float),
        "tag:yaml.org,2002:binary" => binary(&text).map(Value::Binary),
        _ => Err(ManifestError::new(format!("unsupported YAML tag: {name}"))),
    }
}

fn integer(text: &str) -> Result<BigInt> {
    let text = text.replace('_', "");
    let (sign, magnitude) = match text.as_bytes().first() {
        Some(b'-') => (-1, &text[1..]),
        Some(b'+') => (1, &text[1..]),
        _ => (1, text.as_str()),
    };
    let value = if magnitude == "0" {
        BigInt::from(0)
    } else if let Some(digits) = magnitude.strip_prefix("0b") {
        parse_integer(digits, 2)?
    } else if let Some(digits) = magnitude.strip_prefix("0x") {
        parse_integer(digits, 16)?
    } else if magnitude.starts_with('0') {
        let digits = magnitude
            .strip_prefix("0o")
            .or_else(|| magnitude.strip_prefix("0O"))
            .unwrap_or(magnitude);
        parse_integer(digits, 8)?
    } else if magnitude.contains(':') {
        let mut value = BigInt::from(0);
        let mut base = BigInt::from(1);
        for part in magnitude.rsplit(':') {
            value += parse_integer(part, 10)? * &base;
            base *= 60;
        }
        value
    } else {
        parse_integer(magnitude, 10)?
    };
    Ok(value * sign)
}

fn parse_integer(text: &str, radix: u32) -> Result<BigInt> {
    BigInt::parse_bytes(decimal_digits(text).trim().as_bytes(), radix)
        .ok_or_else(|| ManifestError::new("invalid YAML integer"))
}

// Python int/float accept Unicode decimal digits in explicit tags; Rust's
// numeric parsers accept ASCII. Nd decimal blocks consist of ordered groups
// of ten, including adjacent mathematical digit alphabets. Use regex's Unicode
// classification instead of maintaining a second Unicode property table.
fn decimal_digits(text: &str) -> Cow<'_, str> {
    if text.is_ascii() {
        return Cow::Borrowed(text);
    }
    static DECIMAL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\A\p{Nd}\z").unwrap());
    let is_decimal = |character: char| {
        let mut encoded = [0; 4];
        DECIMAL.is_match(character.encode_utf8(&mut encoded))
    };
    Cow::Owned(
        text.chars()
            .map(|character| {
                if character.is_ascii() || !is_decimal(character) {
                    return character;
                }
                let mut start = character as u32;
                while char::from_u32(start - 1).is_some_and(is_decimal) {
                    start -= 1;
                }
                char::from(b'0' + ((character as u32 - start) % 10) as u8)
            })
            .collect(),
    )
}

fn float(text: &str) -> Result<f64> {
    let text = text.replace('_', "").to_lowercase();
    let (sign, magnitude) = match text.as_bytes().first() {
        Some(b'-') => (-1.0, &text[1..]),
        Some(b'+') => (1.0, &text[1..]),
        _ => (1.0, text.as_str()),
    };
    let value = if magnitude == ".inf" {
        f64::INFINITY
    } else if magnitude == ".nan" {
        f64::NAN
    } else if magnitude.contains(':') {
        let mut value = 0.0;
        let mut base = 1.0;
        for part in magnitude.rsplit(':') {
            value += parse_float(part)? * base;
            base *= 60.0;
        }
        value
    } else {
        parse_float(magnitude)?
    };
    Ok(sign * value)
}

fn parse_float(text: &str) -> Result<f64> {
    decimal_digits(text)
        .trim()
        .parse()
        .map_err(|_| ManifestError::new("invalid YAML float"))
}

fn binary(text: &str) -> Result<Vec<u8>> {
    if !text.is_ascii() {
        return Err(ManifestError::new("YAML binary data must be ASCII base64"));
    }
    // Adapt Python base64.decodebytes' ignored ASCII noise and permissive
    // padding, then leave the actual byte decoding to the mature base64 crate.
    let mut normalized = Vec::new();
    let mut padding = 0;
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/') {
            normalized.push(byte);
            padding = 0;
        } else if byte == b'=' {
            match normalized.len() % 4 {
                2 => {
                    padding += 1;
                    if padding == 2 {
                        normalized.extend_from_slice(b"==");
                        break;
                    }
                }
                3 => {
                    normalized.push(b'=');
                    break;
                }
                _ => {}
            }
        }
    }
    let engine = GeneralPurpose::new(
        &alphabet::STANDARD,
        GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
    );
    engine
        .decode(normalized)
        .map_err(|error| ManifestError::new(format!("invalid YAML base64: {error}")))
}

pub fn fields<'a>(
    value: &'a Value,
    allowed: &[&str],
    required: &[&str],
    label: &str,
) -> Result<&'a Mapping> {
    let values = value
        .as_mapping()
        .ok_or_else(|| ManifestError::new(format!("{label} must be a mapping")))?;
    let mut unknown: Vec<_> = values
        .iter()
        .map(|(name, _)| name.as_str())
        .filter(|name| !allowed.contains(name))
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        return Err(ManifestError::new(format!(
            "{label} has unknown fields: {}",
            unknown.join(", ")
        )));
    }
    let mut missing: Vec<_> = required
        .iter()
        .copied()
        .filter(|name| !values.iter().any(|(key, _)| key == name))
        .collect();
    missing.sort_unstable();
    if !missing.is_empty() {
        return Err(ManifestError::new(format!(
            "{label} is missing fields: {}",
            missing.join(", ")
        )));
    }
    Ok(values)
}

pub fn parse_time(value: &Value, label: &str, zero: bool) -> Result<u64> {
    let invalid = || ManifestError::new(format!("{label} must be an integer ns/us/ms/s string"));
    let text = value.as_str().ok_or_else(invalid)?;
    let split = text.bytes().take_while(u8::is_ascii_digit).count();
    if !(1..=13).contains(&split) {
        return Err(invalid());
    }
    let multiplier = match &text[split..] {
        "ns" => 1,
        "us" => 1000,
        "ms" => 1_000_000,
        "s" => 1_000_000_000,
        _ => return Err(invalid()),
    };
    let number = text[..split]
        .parse::<u64>()
        .ok()
        .and_then(|number| number.checked_mul(multiplier))
        .filter(|number| *number <= MAX_TIME_NS && (zero || *number != 0))
        .ok_or_else(|| {
            ManifestError::new(format!(
                "{label} must be {} and at most 3600s",
                if zero { "nonnegative" } else { "positive" }
            ))
        })?;
    Ok(number)
}

pub fn text_bytes(value: &Value, label: &str, empty: bool, maximum: usize) -> Result<Vec<u8>> {
    let text = value
        .as_str()
        .ok_or_else(|| ManifestError::new(format!("{label} must be a string")))?;
    if text.len() > maximum || (!empty && text.is_empty()) {
        return Err(ManifestError::new(format!(
            "{label} must contain {}..{maximum} UTF-8 bytes",
            if empty { 0 } else { 1 }
        )));
    }
    Ok(text.as_bytes().to_vec())
}

pub fn identifier<'a>(value: &'a Value, label: &str) -> Result<&'a str> {
    let valid = |text: &str| {
        (1..=64).contains(&text.len())
            && text.bytes().enumerate().all(|(index, byte)| {
                byte.is_ascii_alphabetic()
                    || byte == b'_'
                    || index > 0 && (byte.is_ascii_digit() || byte == b'-')
            })
    };
    value.as_str().filter(|text| valid(text)).ok_or_else(|| {
        ManifestError::new(format!(
            "{label} must be an ASCII identifier of 1..64 characters"
        ))
    })
}
