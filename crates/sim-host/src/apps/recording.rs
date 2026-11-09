//! Finite experiment records over the existing bounded robot-sim/trace/1 MCAP.
//! The Simulation remains the only scheduler. Replay consumes the complete
//! container before returning any result and never constructs a native backend.

use super::experiment::{
    admission, admit_consumer, run_with_hooks, Consumer, ExperimentResult, Observed, RunError,
};
use crate::{
    assertions::Condition,
    config::{
        scene::{ExperimentAssertion, Origin, ReadyMarker, Scene},
        yaml::Value as Yaml,
    },
    event_json::{event_value, input_json, mapping_json, pure_json, tuple_json},
    runtime::{HostEvent, HostInput},
    trace::{iter_events, TraceEvent, TraceWriter},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Map, Value};
use sim_core::{
    Capability, Direction, Endpoint, Event, EventPayload, Failure, NodeDescription, Observer,
    Result, SimulationOptions, TimeQuality,
};
use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    rc::Rc,
    sync::Arc,
};

const FORMAT: &str = "sim/experiment/2";
const MAX_CONFIG: usize = 8 * 1024 * 1024;
const SEGMENT: usize = 32 * 1024;

fn lower<E: std::error::Error + Send + Sync + 'static>(error: E) -> Failure {
    Arc::new(error)
}

fn python_float(value: f64) -> Result<String> {
    if !value.is_finite() {
        return Err(admission("experiment values must be finite"));
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into());
    }
    // std's shortest round-tripping decimal owns the actual conversion. Adapt
    // only Python repr's fixed/scientific threshold and exponent spelling.
    let shortest = value.abs().to_string();
    let (mantissa, offset) = if let Some((mantissa, exponent)) = shortest.split_once(['e', 'E']) {
        (mantissa, exponent.parse::<i32>().map_err(lower)?)
    } else {
        (shortest.as_str(), 0)
    };
    let point = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let mut digits = mantissa
        .bytes()
        .filter(|b| *b != b'.')
        .map(char::from)
        .collect::<String>();
    let first = digits.bytes().position(|b| b != b'0').unwrap();
    let exponent = point - first as i32 - 1 + offset;
    digits.drain(..first);
    while digits.ends_with('0') {
        digits.pop();
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if !(-4..16).contains(&exponent) {
        let tail = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        Ok(format!(
            "{sign}{}{tail}e{}{exponent:02}",
            &digits[..1],
            if exponent >= 0 { "+" } else { "-" },
            exponent = exponent.abs()
        ))
    } else if exponent < 0 {
        Ok(format!(
            "{sign}0.{}{}",
            "0".repeat((-exponent - 1) as usize),
            digits
        ))
    } else {
        let point = (exponent + 1) as usize;
        if point >= digits.len() {
            Ok(format!(
                "{sign}{digits}{}.0",
                "0".repeat(point - digits.len())
            ))
        } else {
            Ok(format!("{sign}{}.{}", &digits[..point], &digits[point..]))
        }
    }
}
fn normalized(value: &Value) -> Result<Value> {
    Ok(match value {
        Value::Number(number) => {
            let text = if number.is_f64() {
                python_float(
                    number
                        .as_f64()
                        .ok_or_else(|| admission("nonfinite experiment number"))?,
                )?
            } else {
                let text = number.to_string();
                if text.trim_start_matches('-').len() > 78 {
                    return Err(admission("experiment integer exceeds 256 bits"));
                }
                let integer = text.parse::<num_bigint::BigInt>().map_err(lower)?;
                if integer.bits() > 256 {
                    return Err(admission("experiment integer exceeds 256 bits"));
                }
                integer.to_string()
            };
            Value::Number(text.parse().map_err(lower)?)
        }
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| Ok((k.clone(), normalized(v)?)))
                .collect::<Result<_>>()?,
        ),
        Value::Array(array) => Value::Array(array.iter().map(normalized).collect::<Result<_>>()?),
        _ => value.clone(),
    })
}

/// Python's existing format uses compact ensure_ascii JSON and preserves map
/// insertion order. Comparing the re-encoding also rejects duplicate fields.
fn canonical(value: &Value) -> Result<Vec<u8>> {
    let raw = serde_json::to_string(&normalized(value)?).map_err(lower)?;
    let mut output = String::with_capacity(raw.len());
    for character in raw.chars() {
        if character.is_ascii() {
            output.push(character);
        } else {
            for unit in character.encode_utf16(&mut [0; 2]).iter() {
                use std::fmt::Write;
                write!(output, "\\u{unit:04x}").unwrap();
            }
        }
    }
    Ok(output.into_bytes())
}
fn load(payload: &[u8]) -> Result<Value> {
    let value: Value = serde_json::from_slice(payload).map_err(lower)?;
    if canonical(&value)? != payload {
        return Err(admission("noncanonical experiment JSON"));
    }
    Ok(value)
}
fn fields<'a>(value: &'a Value, keys: &[&str]) -> Result<&'a Map<String, Value>> {
    value
        .as_object()
        .filter(|value| value.len() == keys.len() && keys.iter().all(|k| value.contains_key(*k)))
        .ok_or_else(|| admission("invalid experiment record fields"))
}
fn number(value: &Value) -> Result<u64> {
    value
        .as_u64()
        .ok_or_else(|| admission("experiment field requires an unsigned 64-bit integer"))
}
fn string(value: &Value) -> Result<&str> {
    value
        .as_str()
        .ok_or_else(|| admission("experiment field requires a string"))
}
fn tagged<'a>(value: &'a Value, tag: &str) -> Result<&'a Value> {
    let map = fields(value, &["type", "value"])?;
    if map["type"] != tag {
        return Err(admission("unexpected typed experiment value"));
    }
    Ok(&map["value"])
}
fn mapping(value: &Value) -> Result<&Map<String, Value>> {
    tagged(value, "mapping")?
        .as_object()
        .ok_or_else(|| admission("experiment mapping requires an object"))
}
fn tuple(value: &Value) -> Result<&[Value]> {
    tagged(value, "tuple")?
        .as_array()
        .map(Vec::as_slice)
        .ok_or_else(|| admission("experiment tuple requires an array"))
}
fn bytes(value: &Value) -> Result<Vec<u8>> {
    let encoded = string(tagged(value, "bytes")?)?;
    let bytes = STANDARD.decode(encoded).map_err(lower)?;
    if STANDARD.encode(&bytes) != encoded {
        return Err(admission("noncanonical experiment base64"));
    }
    Ok(bytes)
}
fn validate_typed(value: &Value, depth: usize) -> Result<()> {
    if depth > 32 {
        return Err(admission("experiment value nesting exceeds 32 levels"));
    }
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(()),
        Value::Number(_) => {
            normalized(value)?;
            Ok(())
        }
        Value::Object(_) => {
            let map = fields(value, &["type", "value"])?;
            match string(&map["type"])? {
                "bytes" => {
                    bytes(value)?;
                    Ok(())
                }
                "tuple" => {
                    for value in tuple(value)? {
                        validate_typed(value, depth + 1)?;
                    }
                    Ok(())
                }
                "mapping" => {
                    for value in mapping(value)?.values() {
                        validate_typed(value, depth + 1)?;
                    }
                    Ok(())
                }
                "CanFrame" => {
                    frame(value)?;
                    Ok(())
                }
                "ImuSample" => {
                    let values = fields(&map["value"], &["gyro_rad_s", "accel_m_s2"])?;
                    for vector in values.values() {
                        let vector = tuple(vector)?;
                        if vector.len() != 3 {
                            return Err(admission("IMU vectors require three finite numbers"));
                        }
                        for number in vector {
                            finite_number(number)?;
                            validate_typed(number, depth + 2)?;
                        }
                    }
                    Ok(())
                }
                "ImuSignalSample" => {
                    let values = fields(
                        &map["value"],
                        &["sample", "source_time_ns", "mapped_time_ns"],
                    )?;
                    tagged(&values["sample"], "ImuSample")?;
                    validate_typed(&values["sample"], depth + 1)?;
                    if !values["source_time_ns"].is_null() {
                        number(&values["source_time_ns"])?;
                    }
                    number(&values["mapped_time_ns"])?;
                    Ok(())
                }
                "JointState" => {
                    let values = fields(&map["value"], &["position_rad", "velocity_rad_s"])?;
                    for number in values.values() {
                        finite_number(number)?;
                        validate_typed(number, depth + 1)?;
                    }
                    Ok(())
                }
                _ => Err(admission("unsupported typed experiment value")),
            }
        }
        _ => Err(admission("invalid typed experiment value")),
    }
}
fn finite_number(value: &Value) -> Result<()> {
    value
        .as_number()
        .and_then(|v| v.as_f64())
        .filter(|v| v.is_finite())
        .map(|_| ())
        .ok_or_else(|| admission("record requires a finite number"))
}
fn frame(value: &Value) -> Result<crate::can::CanFrame> {
    let map = fields(
        tagged(value, "CanFrame")?,
        &["can_id", "flags", "dlc", "data", "timestamp_ns"],
    )?;
    crate::can::CanFrame::new(
        u32::try_from(number(&map["can_id"])?).map_err(lower)?,
        u32::try_from(number(&map["flags"])?).map_err(lower)?,
        Some(u8::try_from(number(&map["dlc"])?).map_err(lower)?),
        bytes(&map["data"])?,
        number(&map["timestamp_ns"])?,
    )
    .map_err(|e| e as Failure)
}

fn capability(value: &Capability) -> &str {
    match value {
        Capability::Connection => "connection",
        Capability::BindingOwner => "binding_owner",
        Capability::Machine => "machine",
        Capability::Step => "step",
        Capability::Input => "input",
        Capability::InputCommit => "input_commit",
        Capability::Dispatch => "dispatch",
        Capability::Sample => "sample",
        Capability::Health => "health",
        Capability::Custom(value) => value,
    }
}
fn origin(value: &Origin) -> String {
    match value {
        Origin::Session => "session".into(),
        Origin::Ready => "ready".into(),
        Origin::Marker(v) => format!("marker:{v}"),
    }
}
fn trace_config(scene: &Scene, options: SimulationOptions) -> Result<Value> {
    let empty = Default::default();
    let experiment = scene.experiment.as_ref().unwrap_or(&empty);
    admit_consumer(scene, options.clone())?;
    // This owner is never started: public factory/port/input admission precedes
    // the first output byte and native resource acquisition.
    let simulation = scene.simulation(options)?;
    for input in &experiment.inputs {
        simulation.validate_input(&input.endpoint, input.payload.clone())?;
    }
    let descriptions=scene.topology.nodes.iter().zip(simulation.validate()).map(|(node,description)| {
        let mut capabilities=description.capabilities.iter().map(capability).collect::<Vec<_>>(); capabilities.sort();
        (node.name.clone(),mapping_json(json!({
            "capabilities":tuple_json(capabilities.into_iter().map(|v|json!(v)),true),
            "ports":tuple_json(description.ports.iter().map(|port|mapping_json(json!({
                "name":port.name,"protocol":port.protocol,
                "direction":match port.direction {Direction::In=>"in",Direction::Out=>"out",Direction::InOut=>"inout"},
                "capabilities":tuple_json(port.capabilities.iter().map(|v|json!(v)),true)
            }),true)),true)
        }),true))
    }).collect::<Map<_,_>>();
    let assertions=experiment.assertions.iter().map(|item| {
        let assertion=&item.assertion;
        let condition=match &assertion.condition {
            Condition::CanSeen(value)=>{
                let mut map=Map::from_iter([("endpoint".into(),json!(assertion.endpoint)),("id".into(),json!(value.can_id))]);
                if let Some(flags)=value.flags {map.insert("flags".into(),json!(flags));}
                if let Some(dlc)=value.dlc {map.insert("dlc".into(),json!(dlc));}
                if let Some(data)=&value.data {map.insert("data_hex".into(),json!(data.iter().map(|b|format!("{b:02x}")).collect::<String>()));}
                Value::Object(map)
            },
            Condition::UartContains(value)=>json!({"endpoint":assertion.endpoint,"text":std::str::from_utf8(value).map_err(lower)?}),
            Condition::DigitalEquals(value)=>json!({"endpoint":assertion.endpoint,"value":u8::from(*value)}),
        };
        let mut map=Map::from_iter([("before".into(),json!(format!("{}ns",assertion.before_ns))),
            ("origin".into(),json!(origin(&item.origin)))]);
        map.insert(assertion.kind().into(),mapping_json(condition,true));
        Ok(mapping_json(Value::Object(map),true))
    }).collect::<Result<Vec<_>>>()?;
    let connected = scene
        .topology
        .links
        .iter()
        .flat_map(|link| [link.source.to_string(), link.target.to_string()])
        .collect::<BTreeSet<_>>();
    Ok(mapping_json(
        json!({
            "duration_ns":scene.duration_ns,"quantum_ns":scene.quantum_ns,
            "ready":tuple_json(experiment.ready.iter().map(|ready|mapping_json(json!({
                "name":ready.name,"endpoint":ready.endpoint.to_string(),"text":crate::event_json::bytes_json(&ready.text,true)
            }),true)),true),
            "assertions":tuple_json(assertions,true),"descriptions":mapping_json(Value::Object(descriptions),true),
            "connected":tuple_json(connected.into_iter().map(|v|json!(v)),true),
            "inputs":tuple_json(experiment.inputs.iter().map(|input|mapping_json(json!({
                "at_ns":input.at_ns,"endpoint":input.endpoint.to_string(),"payload":input_json(&input.payload,true),
                "origin":origin(&input.origin)
            }),true)),true)
        }),
        true,
    ))
}

struct Recorder<W: Write> {
    writer: TraceWriter<W>,
    ordinal: u64,
    events: u64,
    cuts: u64,
    last_time: u64,
    last_cut: Option<u64>,
}
impl<W: Write> Recorder<W> {
    fn new(output: W, config: Value) -> Result<Self> {
        validate_typed(&config, 0)?;
        let encoded = canonical(&config)?;
        if encoded.is_empty() || encoded.len() > MAX_CONFIG {
            return Err(admission("experiment configuration exceeds 8MiB"));
        }
        let count = encoded.len().div_ceil(SEGMENT);
        let mut owner = Self {
            writer: TraceWriter::new(output).map_err(|e| e as Failure)?,
            ordinal: 0,
            events: 0,
            cuts: 0,
            last_time: 0,
            last_cut: None,
        };
        for (index, segment) in encoded.chunks(SEGMENT).enumerate() {
            owner.append("config",json!({"index":index,"count":count,"size":encoded.len(),"data":STANDARD.encode(segment)}),0)?;
        }
        Ok(owner)
    }
    fn append(&mut self, kind: &str, data: Value, time: u64) -> Result<()> {
        let payload =
            canonical(&json!({"format":FORMAT,"kind":kind,"ordinal":self.ordinal,"data":data}))?;
        let event = TraceEvent::new(
            time,
            "simulation",
            "experiment",
            FORMAT,
            payload,
            "simulation",
        )
        .map_err(|e| e as Failure)?;
        self.writer.append(&event).map_err(|e| e as Failure)?;
        self.ordinal = self
            .ordinal
            .checked_add(1)
            .ok_or_else(|| admission("experiment record count overflow"))?;
        Ok(())
    }
    fn event(&mut self, event: &Event<HostInput, HostEvent>) -> Result<()> {
        if event.session_id != 1
            || event.sequence != self.events
            || event.delivery_time_ns < self.last_time
        {
            return Err(admission(
                "invalid experiment Event sequence/session/delivery",
            ));
        }
        self.append("event", event_value(event, true), event.delivery_time_ns)?;
        self.events += 1;
        self.last_time = event.delivery_time_ns;
        Ok(())
    }
    fn cut(&mut self, cut: u64) -> Result<()> {
        if cut < self.last_time {
            return Err(admission("experiment cuts moved backwards"));
        }
        self.append("cut", json!(cut), cut)?;
        self.cuts += 1;
        self.last_cut = Some(cut);
        self.last_time = cut;
        Ok(())
    }
    fn complete(&mut self, result: &ExperimentResult) -> Result<()> {
        if self.last_cut != Some(self.last_time) {
            return Err(admission("experiment completion requires its last cut"));
        }
        let summary = pure_json(
            &json!({"end_ns":result.end_ns,"origin_ns":result.origin_ns,
            "delivered_inputs":result.delivered_inputs,"events_total":result.events_total,
            "cuts_total":self.cuts,
            "ready_receipts":result.ready_receipts.iter().map(|(k,v)|(k.clone(),crate::event_json::receipt_json(v))).collect::<Map<_,_>>(),
            "boards":result.boards}),
            true,
        );
        self.append("completion",json!({"events":self.events,"cuts":self.cuts,"records":self.ordinal+1,
            "session_id":if self.events==0 {Value::Null}else{json!(1)},"last_time_ns":self.last_time,"summary":summary}),self.last_time)?;
        self.writer.finish().map_err(|e| e as Failure)
    }
}

/// Own the supplied output for this call. Passing an owned File streams without
/// retaining event history. Storage also accepts a borrowed stream independently.
pub fn run_recorded<W: Write + 'static>(
    scene: &Scene,
    options: SimulationOptions,
    output: W,
    on_event: Option<Observer<HostInput, HostEvent>>,
) -> std::result::Result<ExperimentResult, RunError> {
    let fail = |primary| RunError {
        primary,
        notes: vec![],
        time_ns: 0,
        events_total: 0,
    };
    let config = trace_config(scene, options.clone()).map_err(fail)?;
    let recorder = Rc::new(RefCell::new(Recorder::new(output, config).map_err(fail)?));
    let event_sink = recorder.clone();
    let cut_sink = recorder.clone();
    let result = run_with_hooks(
        scene,
        options,
        on_event,
        Some(Rc::new(move |cut| cut_sink.borrow_mut().cut(cut))),
        Some(Rc::new(move |event, _| {
            event_sink.borrow_mut().event(event)?;
            Ok(())
        })),
    )?;
    recorder
        .borrow_mut()
        .complete(&result)
        .map_err(|primary| RunError {
            primary,
            notes: vec![],
            time_ns: result.end_ns,
            events_total: result.events_total,
        })?;
    Ok(result)
}

fn typed_yaml(value: &Value) -> Result<Yaml> {
    Ok(match value {
        Value::Null => Yaml::Null,
        Value::Bool(v) => Yaml::Bool(*v),
        Value::String(v) => Yaml::String(v.clone()),
        Value::Number(v) => {
            if v.is_f64() {
                Yaml::Float(
                    v.as_f64()
                        .ok_or_else(|| admission("nonfinite experiment value"))?,
                )
            } else {
                Yaml::Integer(v.to_string().parse().map_err(lower)?)
            }
        }
        _ => match string(&fields(value, &["type", "value"])?["type"])? {
            "bytes" => Yaml::Binary(bytes(value)?),
            "tuple" => Yaml::Sequence(
                tuple(value)?
                    .iter()
                    .map(typed_yaml)
                    .collect::<Result<_>>()?,
            ),
            "mapping" => Yaml::Mapping(
                mapping(value)?
                    .iter()
                    .map(|(k, v)| Ok((k.clone(), typed_yaml(v)?)))
                    .collect::<Result<_>>()?,
            ),
            _ => {
                return Err(admission(
                    "record is not a configuration scalar or collection",
                ));
            }
        },
    })
}

fn untyped_json(value: &Value) -> Result<Value> {
    Ok(match value {
        Value::Null | Value::Bool(_) | Value::String(_) | Value::Number(_) => value.clone(),
        _ => match string(&fields(value, &["type", "value"])?["type"])? {
            "mapping" => Value::Object(
                mapping(value)?
                    .iter()
                    .map(|(k, v)| Ok((k.clone(), untyped_json(v)?)))
                    .collect::<Result<_>>()?,
            ),
            "tuple" => Value::Array(
                tuple(value)?
                    .iter()
                    .map(untyped_json)
                    .collect::<Result<_>>()?,
            ),
            _ => {
                return Err(admission(
                    "native snapshot contains an unsupported typed value",
                ));
            }
        },
    })
}

struct ReplayPlan {
    duration: u64,
    quantum: u64,
    descriptions: BTreeMap<String, NodeDescription>,
    consumer: Consumer,
}
fn unique_strings(value: &Value, maximum: usize) -> Result<BTreeSet<String>> {
    let values = tuple(value)?;
    let mut set = BTreeSet::new();
    if values.len() > maximum {
        return Err(admission("experiment collection exceeds its bound"));
    }
    for value in values {
        let value = string(value)?;
        if value.is_empty() || value.contains('\0') || !set.insert(value.to_owned()) {
            return Err(admission(
                "experiment descriptions require unique nonempty strings",
            ));
        }
    }
    Ok(set)
}
fn replay_plan(config: &Value, replacement: Option<&Yaml>) -> Result<ReplayPlan> {
    let config = mapping(config)?;
    if config.len() != 7
        || ![
            "duration_ns",
            "quantum_ns",
            "ready",
            "assertions",
            "descriptions",
            "connected",
            "inputs",
        ]
        .iter()
        .all(|k| config.contains_key(*k))
    {
        return Err(admission("invalid experiment configuration fields"));
    }
    let duration = number(&config["duration_ns"])?;
    let quantum = number(&config["quantum_ns"])?;
    if duration == 0 || quantum == 0 {
        return Err(admission(
            "experiment duration and quantum must be positive",
        ));
    }
    let raw = mapping(&config["descriptions"])?;
    if !(1..=63).contains(&raw.len()) {
        return Err(admission(
            "experiment trace requires 1..63 node descriptions",
        ));
    }
    let mut descriptions = BTreeMap::new();
    for (name, value) in raw {
        Endpoint::new(name, "_recorded")?;
        let map = mapping(value)?;
        if map.len() != 2 || !map.contains_key("capabilities") || !map.contains_key("ports") {
            return Err(admission("invalid recorded node description"));
        }
        let capabilities = unique_strings(&map["capabilities"], 128)?
            .into_iter()
            .map(|v| match v.as_str() {
                "connection" => Capability::Connection,
                "machine" => Capability::Machine,
                "input" => Capability::Input,
                _ => Capability::Custom(v),
            });
        let mut ports = vec![];
        let mut names = BTreeSet::new();
        for value in tuple(&map["ports"])? {
            let port = mapping(value)?;
            if port.len() != 4
                || !["name", "protocol", "direction", "capabilities"]
                    .iter()
                    .all(|k| port.contains_key(*k))
            {
                return Err(admission("invalid recorded port description"));
            }
            let port_name = string(&port["name"])?;
            Endpoint::new(name, port_name)?;
            if !names.insert(port_name.to_owned()) {
                return Err(admission("duplicate recorded port name"));
            }
            let protocol = string(&port["protocol"])?;
            if protocol.is_empty() || protocol.contains('\0') {
                return Err(admission("invalid recorded port protocol"));
            }
            let mut descriptor = sim_core::PortSpec::new(port_name, protocol);
            descriptor.direction = match string(&port["direction"])? {
                "in" => Direction::In,
                "out" => Direction::Out,
                "inout" => Direction::InOut,
                _ => return Err(admission("invalid recorded port direction")),
            };
            descriptor.capabilities = unique_strings(&port["capabilities"], 128)?;
            ports.push(descriptor);
        }
        descriptions.insert(name.clone(), NodeDescription::new(ports, capabilities));
    }
    let connected = unique_strings(&config["connected"], 512)?;
    for value in &connected {
        let endpoint = Endpoint::parse(value)?;
        descriptions
            .get(&endpoint.node)
            .ok_or_else(|| admission("recorded connection names unknown node"))?
            .port(&endpoint.port)?;
    }
    let mut ready = vec![];
    for value in tuple(&config["ready"])? {
        let item = mapping(value)?;
        if item.len() != 3
            || !["name", "endpoint", "text"]
                .iter()
                .all(|k| item.contains_key(*k))
        {
            return Err(admission("invalid recorded ready declaration"));
        }
        ready.push(ReadyMarker {
            name: string(&item["name"])?.to_owned(),
            endpoint: Endpoint::parse(string(&item["endpoint"])?)?,
            text: bytes(&item["text"])?,
        });
    }
    let inputs = tuple(&config["inputs"])?;
    if inputs.len() > 1024 {
        return Err(admission("too many recorded inputs"));
    }
    for value in inputs {
        let item = mapping(value)?;
        if item.len() != 4
            || !["at_ns", "endpoint", "payload", "origin"]
                .iter()
                .all(|k| item.contains_key(*k))
        {
            return Err(admission("invalid recorded input declaration"));
        }
        let endpoint = Endpoint::parse(string(&item["endpoint"])?)?;
        let description = descriptions
            .get(&endpoint.node)
            .ok_or_else(|| admission("recorded input names unknown node"))?;
        let port = description.port(&endpoint.port)?;
        let input_origin = string(&item["origin"])?;
        let valid_origin = matches!(input_origin, "ready" | "session")
            || input_origin
                .strip_prefix("marker:")
                .is_some_and(|name| ready.iter().any(|r| r.name == name));
        if number(&item["at_ns"])? > duration
            || !valid_origin
            || !description.has(Capability::Input)
            || port.direction == Direction::Out
            || !port.capabilities.contains("external_input")
        {
            return Err(admission(
                "invalid recorded input origin, endpoint or horizon",
            ));
        }
    }
    let admit_assertions = |values: &Yaml| -> Result<Vec<ExperimentAssertion>> {
        let values = values
            .as_sequence()
            .filter(|v| v.len() <= 1024)
            .ok_or_else(|| admission("experiment requires at most 1024 assertion mappings"))?;
        values
            .iter()
            .enumerate()
            .map(|(index, value)| {
                let map = value
                    .as_mapping()
                    .ok_or_else(|| admission("assertion requires a mapping"))?;
                if map
                    .iter()
                    .find(|(k, _)| k == "origin")
                    .is_some_and(|(_, v)| !matches!(v.as_str(), Some("session" | "ready")))
                {
                    return Err(admission("invalid assertion origin"));
                }
                let value =
                    Yaml::Mapping(map.iter().filter(|(k, _)| k != "origin").cloned().collect());
                let assertion =
                    crate::config::assertions::parse_assertion(&value, index, |_, text| {
                        Ok(text.to_owned())
                    })
                    .map_err(lower)?;
                if assertion.before_ns > duration {
                    return Err(admission("assertion deadline exceeds experiment duration"));
                }
                let origin = if map
                    .iter()
                    .find(|(k, _)| k == "origin")
                    .is_some_and(|(_, v)| v.as_str() == Some("session"))
                {
                    Origin::Session
                } else {
                    Origin::Ready
                };
                Ok(ExperimentAssertion { assertion, origin })
            })
            .collect()
    };
    let originals = typed_yaml(&config["assertions"])?;
    let mut assertions = admit_assertions(&originals)?;
    Consumer::new(
        assertions.clone(),
        duration,
        ready.clone(),
        &descriptions,
        &connected,
    )?;
    if let Some(replacement) = replacement {
        let values = if let Yaml::Mapping(map) = replacement {
            if map.len() != 1 || map[0].0 != "assertions" {
                return Err(admission("replacement document requires only assertions"));
            }
            &map[0].1
        } else {
            replacement
        };
        assertions = admit_assertions(values)?;
    }
    let consumer = Consumer::new(assertions, duration, ready, &descriptions, &connected)?;
    Ok(ReplayPlan {
        duration,
        quantum,
        descriptions,
        consumer,
    })
}

fn recorded_event(value: &Value) -> Result<Event<HostInput, Value>> {
    let map = fields(
        value,
        &[
            "session_id",
            "sequence",
            "source_node",
            "source_port",
            "kind",
            "payload",
            "source_time_ns",
            "mapped_time_ns",
            "delivery_time_ns",
            "quality",
        ],
    )?;
    validate_typed(&map["payload"], 0)?;
    let source_node = string(&map["source_node"])?;
    let kind = string(&map["kind"])?;
    let source_port = if map["source_port"].is_null() {
        None
    } else {
        Some(string(&map["source_port"])?.to_owned())
    };
    if source_node.is_empty()
        || kind.is_empty()
        || source_port.as_ref().is_some_and(|v| v.is_empty())
    {
        return Err(admission(
            "event requires a source node, optional port and kind",
        ));
    }
    let session_id = number(&map["session_id"])?;
    if session_id == 0 {
        return Err(admission("event session must be positive"));
    }
    Ok(Event {
        session_id,
        sequence: number(&map["sequence"])?,
        source_node: source_node.to_owned(),
        source_port,
        kind: kind.to_owned(),
        payload: EventPayload::Observation(map["payload"].clone()),
        source_time_ns: if map["source_time_ns"].is_null() {
            None
        } else {
            Some(number(&map["source_time_ns"])?)
        },
        mapped_time_ns: number(&map["mapped_time_ns"])?,
        delivery_time_ns: number(&map["delivery_time_ns"])?,
        quality: match string(&map["quality"])? {
            "exact" => TimeQuality::Exact,
            "sampled" => TimeQuality::Sampled,
            "lower_bound" => TimeQuality::LowerBound,
            "upper_bound" => TimeQuality::UpperBound,
            _ => return Err(admission("unknown event time quality")),
        },
    })
}

/// Evaluate one physical MCAP stream through CRC, footer and EOF exactly once.
/// No native process, simulation resource or input side effect is constructed.
pub fn replay_experiment<R: Read + ?Sized>(
    source: &mut R,
    replacement: Option<&Yaml>,
) -> Result<ExperimentResult> {
    let mut plan: Option<ReplayPlan> = None;
    let mut config = Vec::new();
    let mut config_index = 0;
    let mut config_count = 0;
    let mut config_size = 0;
    let mut ordinal = 0;
    let mut events = 0;
    let mut cuts = 0;
    let mut last_time = 0;
    let mut last_cut = None;
    let mut pending_cut = None;
    let mut summary = None;
    for item in iter_events(source) {
        let item = item.map_err(|e| e as Failure)?;
        if item.source() != "simulation"
            || item.endpoint() != "experiment"
            || item.event_type() != FORMAT
            || item.clock() != "simulation"
        {
            return Err(admission("unexpected experiment MCAP identity"));
        }
        let value = load(item.payload())?;
        let record = fields(&value, &["format", "kind", "ordinal", "data"])?;
        if record["format"] != FORMAT || number(&record["ordinal"])? != ordinal || summary.is_some()
        {
            return Err(admission("invalid experiment record order/schema"));
        }
        ordinal += 1;
        let kind = string(&record["kind"])?;
        let data = &record["data"];
        if plan.is_none() {
            let segment = fields(data, &["index", "count", "size", "data"])?;
            let size = number(&segment["size"])? as usize;
            let count = number(&segment["count"])? as usize;
            if kind != "config"
                || item.timestamp_ns() != 0
                || number(&segment["index"])? as usize != config_index
                || size == 0
                || size > MAX_CONFIG
                || count != size.div_ceil(SEGMENT)
            {
                return Err(admission("invalid experiment config segment"));
            }
            if config_index == 0 {
                config_count = count;
                config_size = size;
            }
            if count != config_count || size != config_size {
                return Err(admission("inconsistent experiment config segments"));
            }
            let encoded = string(&segment["data"])?;
            let segment = STANDARD.decode(encoded).map_err(lower)?;
            if segment.len() != SEGMENT.min(size - config.len())
                || STANDARD.encode(&segment) != encoded
            {
                return Err(admission("invalid experiment config segment size/encoding"));
            }
            config.extend(segment);
            config_index += 1;
            if config_index == config_count {
                let value = load(&config)?;
                validate_typed(&value, 0)?;
                plan = Some(replay_plan(&value, replacement)?);
                config.clear();
            }
            continue;
        }
        let plan = plan.as_mut().unwrap();
        match kind {
            "event" => {
                let event = recorded_event(data)?;
                let descriptor = plan
                    .descriptions
                    .get(&event.source_node)
                    .ok_or_else(|| admission("experiment Event names unknown node"))?;
                if let Some(port) = &event.source_port {
                    descriptor.port(port)?;
                }
                if event.sequence != events
                    || event.session_id != 1
                    || event.delivery_time_ns < last_time
                    || item.timestamp_ns() != event.delivery_time_ns
                {
                    return Err(admission(
                        "invalid experiment Event sequence/session/delivery",
                    ));
                }
                if last_cut.is_none_or(|cut| event.delivery_time_ns > cut) {
                    if pending_cut.is_some_and(|cut| cut != event.delivery_time_ns) {
                        return Err(admission("experiment trace lacks an observation cut"));
                    }
                    pending_cut = Some(event.delivery_time_ns);
                }
                let EventPayload::Observation(payload) = &event.payload else {
                    unreachable!()
                };
                let can = match event.kind.as_str() {
                    "can.delivery" => {
                        let value = mapping(payload)?;
                        Some(frame(
                            value
                                .get("frame")
                                .ok_or_else(|| admission("CAN delivery lacks frame"))?,
                        )?)
                    }
                    "can-tx" => Some(frame(payload)?),
                    _ => None,
                };
                let uart = match event.kind.as_str() {
                    "uart" => Some(bytes(payload)?),
                    "net.uart.delivery" => Some(bytes(
                        mapping(payload)?
                            .get("data")
                            .ok_or_else(|| admission("UART delivery lacks data"))?,
                    )?),
                    _ => None,
                };
                let snapshot = if event.kind == "snapshot" {
                    Some(untyped_json(payload)?)
                } else {
                    None
                };
                let observed = if let Some(can) = &can {
                    let dropped = if event.kind == "can.delivery" {
                        mapping(payload)?
                            .get("dropped")
                            .and_then(Value::as_bool)
                            .ok_or_else(|| admission("CAN delivery requires a boolean drop flag"))?
                    } else {
                        false
                    };
                    Observed::Can(can, dropped)
                } else if let Some(uart) = &uart {
                    let source = if event.kind == "net.uart.delivery" {
                        Some(string(
                            mapping(payload)?
                                .get("source")
                                .ok_or_else(|| admission("UART delivery lacks source"))?,
                        )?)
                    } else {
                        None
                    };
                    Observed::Uart(uart, source)
                } else if let Some(snapshot) = &snapshot {
                    Observed::Snapshot(snapshot)
                } else if matches!(event.kind.as_str(), "digital" | "net.digital.sample") {
                    let level = if event.kind == "digital" {
                        payload
                    } else {
                        mapping(payload)?
                            .get("level")
                            .ok_or_else(|| admission("digital sample lacks level"))?
                    };
                    Observed::Digital(if level.is_null() {
                        None
                    } else {
                        Some(match number(level)? {
                            0 => false,
                            1 => true,
                            _ => return Err(admission("digital level requires 0 or 1")),
                        })
                    })
                } else {
                    Observed::None
                };
                plan.consumer.observe(&event, observed)?;
                events += 1;
                last_time = event.delivery_time_ns;
            }
            "cut" => {
                let cut = number(data)?;
                if item.timestamp_ns() != cut
                    || cut < last_time
                    || last_cut.map_or(cut != 0, |previous| {
                        cut < previous || cut - previous > plan.quantum
                    })
                    || pending_cut.is_some_and(|pending| pending != cut)
                {
                    return Err(admission(
                        "experiment trace observation cuts differ from its runtime",
                    ));
                }
                plan.consumer.begin_cut(cut)?;
                last_cut = Some(cut);
                last_time = cut;
                pending_cut = None;
                cuts += 1;
            }
            "completion" => {
                let completion = fields(
                    data,
                    &[
                        "events",
                        "cuts",
                        "records",
                        "session_id",
                        "last_time_ns",
                        "summary",
                    ],
                )?;
                if number(&completion["events"])? != events
                    || number(&completion["cuts"])? != cuts
                    || number(&completion["records"])? != ordinal
                    || completion["session_id"] != if events == 0 { Value::Null } else { json!(1) }
                    || number(&completion["last_time_ns"])? != last_time
                    || last_cut != Some(last_time)
                    || item.timestamp_ns() != last_time
                {
                    return Err(admission(
                        "invalid experiment completion counts/time/session",
                    ));
                }
                validate_typed(&completion["summary"], 0)?;
                summary = Some(completion["summary"].clone());
            }
            _ => return Err(admission("unsupported experiment record kind")),
        }
    }
    // Only here have the native reader's CRC/footer/trailing magic/EOF gates
    // all succeeded. A yielded completion record alone never publishes results.
    let mut plan = plan.ok_or_else(|| admission("incomplete experiment trace"))?;
    let summary = summary.ok_or_else(|| admission("incomplete experiment trace"))?;
    let summary = mapping(&summary)?;
    if summary.len() != 7
        || ![
            "end_ns",
            "origin_ns",
            "delivered_inputs",
            "events_total",
            "cuts_total",
            "ready_receipts",
            "boards",
        ]
        .iter()
        .all(|k| summary.contains_key(*k))
    {
        return Err(admission("invalid experiment completion summary"));
    }
    let end = number(&summary["end_ns"])?;
    let result = plan.consumer.result(end, events);
    let origin = plan
        .consumer
        .origin_ns
        .ok_or_else(|| admission("experiment trace lacks completed ready lifecycle"))?;
    let expected_ready = pure_json(
        &Value::Object(
            result
                .ready_receipts
                .iter()
                .map(|(k, v)| (k.clone(), crate::event_json::receipt_json(v)))
                .collect(),
        ),
        true,
    );
    let expected_boards = pure_json(&json!(result.boards), true);
    if pending_cut.is_some()
        || last_cut != Some(end)
        || Some(end) != origin.checked_add(plan.duration)
        || number(&summary["origin_ns"])? != origin
        || number(&summary["delivered_inputs"])? != result.delivered_inputs
        || number(&summary["events_total"])? != events
        || number(&summary["cuts_total"])? != cuts
        || summary["ready_receipts"] != expected_ready
        || summary["boards"] != expected_boards
    {
        return Err(admission(
            "experiment completion differs from captured lifecycle",
        ));
    }
    plan.consumer.finish(end)?;
    Ok(plan.consumer.result(end, events))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_rejects_duplicate_fields_and_noncanonical_numeric_or_text_spelling() {
        for payload in [
            br#"{"same":1,"same":2}"#.as_slice(),
            br#"{"a": 1}"#,
            br#"[1e-6]"#,
            br#"[1E-06]"#,
            br#"[-0]"#,
            br#"[1e999]"#,
            "[\"你好\"]".as_bytes(),
        ] {
            assert!(
                load(payload).is_err(),
                "accepted {}",
                String::from_utf8_lossy(payload)
            );
        }
        assert_eq!(
            canonical(&load(br#"[1e-06,1e+16,-0.0,0.0001]"#).unwrap()).unwrap(),
            br#"[1e-06,1e+16,-0.0,0.0001]"#
        );
    }
}
