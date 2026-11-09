//! Public event/report values shared by CLI output and experiment recording.

use crate::{
    apps::experiment::{ExperimentResult, ObservationReceipt},
    assertions::Status,
    can::CanFrame,
    runtime::{HostEvent, HostInput},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Map, Value};
use sim_core::{Event, EventPayload, TimeQuality};

pub fn quality_name(value: TimeQuality) -> &'static str {
    match value {
        TimeQuality::Exact => "exact",
        TimeQuality::Sampled => "sampled",
        TimeQuality::LowerBound => "lower_bound",
        TimeQuality::UpperBound => "upper_bound",
    }
}

pub(crate) fn typed(tag: &str, value: Value) -> Value {
    json!({"type":tag,"value":value})
}

pub(crate) fn bytes_json(bytes: &[u8], trace: bool) -> Value {
    if trace {
        typed("bytes", Value::String(STANDARD.encode(bytes)))
    } else {
        json!({"hex": bytes.iter().map(|byte| format!("{byte:02x}")).collect::<String>()})
    }
}

pub(crate) fn mapping_json(value: Value, trace: bool) -> Value {
    if trace {
        typed("mapping", value)
    } else {
        value
    }
}

pub(crate) fn tuple_json(values: impl IntoIterator<Item = Value>, trace: bool) -> Value {
    let value = Value::Array(values.into_iter().collect());
    if trace {
        typed("tuple", value)
    } else {
        value
    }
}

pub(crate) fn frame_json(frame: &CanFrame, trace: bool) -> Value {
    let value = json!({
        "can_id":frame.can_id(),"flags":frame.flags(),"dlc":frame.dlc(),
        "data":bytes_json(frame.data(), trace),"timestamp_ns":frame.timestamp_ns()
    });
    if trace {
        typed("CanFrame", value)
    } else {
        value
    }
}

fn imu_json(sample: &crate::signals::ImuSample, trace: bool) -> Value {
    let value = json!({
        "gyro_rad_s":tuple_json(sample.gyro_rad_s.into_iter().map(|v|json!(v)),trace),
        "accel_m_s2":tuple_json(sample.accel_m_s2.into_iter().map(|v|json!(v)),trace)
    });
    if trace {
        typed("ImuSample", value)
    } else {
        value
    }
}

fn imu_signal_json(sample: &crate::signals::ImuSignalSample, trace: bool) -> Value {
    let value = json!({"sample":imu_json(&sample.sample,trace),"source_time_ns":sample.source_time_ns,"mapped_time_ns":sample.mapped_time_ns});
    if trace {
        typed("ImuSignalSample", value)
    } else {
        value
    }
}

pub(crate) fn input_json(input: &HostInput, trace: bool) -> Value {
    match input {
        HostInput::CanFrame(frame) => frame_json(frame, trace),
        HostInput::FaultProfile(value) => json!(value),
        #[cfg(feature = "referee")]
        HostInput::RefereeEnable(value) => json!(value),
        #[cfg(feature = "physics")]
        HostInput::JointEffort(values) => mapping_json(
            Value::Object(values.iter().map(|(k, v)| (k.clone(), json!(v))).collect()),
            trace,
        ),
        #[cfg(all(target_os = "linux", feature = "machine"))]
        HostInput::Machine(value) => match value {
            crate::profile::MachineInput::Bool(value) => json!(value),
            crate::profile::MachineInput::Level(value) => json!(u8::from(*value)),
            crate::profile::MachineInput::Bytes(value) => bytes_json(value, trace),
            crate::profile::MachineInput::AdcRaw(value) => {
                mapping_json(json!({"raw":value}), trace)
            }
            crate::profile::MachineInput::AdcVoltage(value) => {
                mapping_json(json!({"voltage_uv":value}), trace)
            }
        },
    }
}

/// Encode an arbitrary native snapshot without changing its public structure.
#[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
pub(crate) fn pure_json(value: &Value, trace: bool) -> Value {
    if !trace {
        return value.clone();
    }
    match value {
        Value::Object(value) => mapping_json(
            Value::Object(
                value
                    .iter()
                    .map(|(k, v)| (k.clone(), pure_json(v, trace)))
                    .collect(),
            ),
            trace,
        ),
        Value::Array(value) => tuple_json(value.iter().map(|v| pure_json(v, trace)), trace),
        _ => value.clone(),
    }
}

fn payload_json(event: &Event<HostInput, HostEvent>, trace: bool) -> Value {
    let value = match &event.payload {
        EventPayload::Input(value) => return input_json(value, trace),
        EventPayload::Observation(value) => value,
    };
    let fields = match value {
        #[cfg(feature = "referee")]
        HostEvent::RefereeBatch {
            data,
            first_sequence,
            next_sequence,
            publication,
            period_ns,
            due_ns,
            next_due_ns,
        } => json!({
            "data":bytes_json(data,trace),"first_sequence":first_sequence,"next_sequence":next_sequence,
            "publication":publication,"period_ns":period_ns,"due_ns":due_ns,"next_due_ns":next_due_ns
        }),

        HostEvent::ImuSample { sample } => json!({"sample":imu_signal_json(sample,trace)}),
        HostEvent::ImuDelivery {
            source,
            destination,
            sample,
            sequence,
            due_ns,
        } => json!({
            "source":source,"destination":destination,"sample":imu_signal_json(sample,trace),
            "sequence":sequence,"due_ns":due_ns
        }),
        #[cfg(all(target_os = "linux", feature = "host-processes"))]
        HostEvent::ProcessStarted { name, pid } => json!({"name":name,"pid":pid}),
        #[cfg(all(target_os = "linux", feature = "host-processes"))]
        HostEvent::ProcessOutput {
            name,
            stdout,
            stderr,
        } => {
            json!({"name":name,"stdout":stdout,"stderr":stderr})
        }
        #[cfg(feature = "physics")]
        HostEvent::PhysicsState(value) => {
            let joints=value.joints.iter().map(|(name,sample)| {
                let value=json!({"position_rad":sample.position_rad,"velocity_rad_s":sample.velocity_rad_s});
                (name.clone(),if trace {typed("JointState",value)}else{value})
            }).collect::<Map<_,_>>();
            let mut fields = Map::from_iter([
                ("native_time_s".into(), json!(value.native_time_s)),
                ("joints".into(), mapping_json(Value::Object(joints), trace)),
            ]);
            if !value.imus.is_empty() {
                fields.insert(
                    "imus".into(),
                    mapping_json(
                        Value::Object(
                            value
                                .imus
                                .iter()
                                .map(|(name, sample)| (name.clone(), imu_json(sample, trace)))
                                .collect(),
                        ),
                        trace,
                    ),
                );
            }
            Value::Object(fields)
        }
        #[cfg(feature = "devices")]
        HostEvent::M3508State {
            current_raw,
            rejected_commands,
        } => json!({"current_raw":current_raw,"rejected_commands":rejected_commands}),
        HostEvent::CanSent { frame } | HostEvent::CanHostReceived { frame } => {
            json!({"frame":frame_json(frame,trace)})
        }
        HostEvent::CanDelivery {
            destination,
            frame,
            sequence,
            due_ns,
            late,
            dropped,
        } => json!({
            "destination":destination,"frame":frame_json(frame,trace),
            "bus_sequence":sequence,"due_ns":due_ns,"late":late,"dropped":dropped
        }),
        HostEvent::CanFaultApplied {
            previous_profile,
            profile,
            delay_ns,
            drop,
            cut_ns,
        } => {
            let rules = drop.iter().map(|rule| {
                let mut value = Map::new();
                if let Some(source) = &rule.source {
                    value.insert("source".into(), json!(source.to_string()));
                }
                if let Some(destination) = &rule.destination {
                    value.insert("destination".into(), json!(destination.to_string()));
                }
                if let Some(id) = rule.can_id {
                    value.insert("can_id".into(), json!(id));
                }
                mapping_json(Value::Object(value), trace)
            });
            json!({"previous_profile":previous_profile,"profile":profile,"delay_ns":delay_ns,
                "drop":tuple_json(rules,trace),"cut_ns":cut_ns})
        }
        HostEvent::DigitalSample {
            source,
            level,
            active,
            changed,
        } => json!({
            "source":source,"level":level.map(u8::from),"active":active,"changed":changed
        }),
        HostEvent::DigitalEdgeDelivery {
            source,
            destination,
            level,
            sequence,
            due_ns,
        }
        | HostEvent::DigitalDelivery {
            source,
            destination,
            level,
            sequence,
            due_ns,
        } => json!({
            "source":source,"destination":destination,"level":u8::from(*level),
            "sequence":sequence,"due_ns":due_ns
        }),
        HostEvent::UartSent {
            source,
            data,
            sequence,
            due_ns,
        } => json!({
            "source":source,"data":bytes_json(data,trace),"sequence":sequence,"due_ns":due_ns
        }),
        HostEvent::UartDelivery {
            source,
            destination,
            data,
            sequence,
            due_ns,
        } => json!({
            "source":source,"destination":destination,"data":bytes_json(data,trace),
            "sequence":sequence,"due_ns":due_ns
        }),
        #[cfg(all(target_os = "linux", feature = "machine"))]
        HostEvent::Snapshot { value }
        | HostEvent::CanInputProcessed { value }
        | HostEvent::Cosim { value } => return pure_json(value, trace),
        #[cfg(all(target_os = "linux", feature = "machine"))]
        HostEvent::MachineUart { data } => return bytes_json(data, trace),
        #[cfg(all(target_os = "linux", feature = "machine"))]
        HostEvent::MachineCanTx { frame } => return frame_json(frame, trace),
    };
    mapping_json(fields, trace)
}

pub(crate) fn event_value(event: &Event<HostInput, HostEvent>, trace: bool) -> Value {
    json!({
        "session_id":event.session_id,"sequence":event.sequence,"source_node":event.source_node,
        "source_port":event.source_port,"kind":event.kind,"payload":payload_json(event,trace),
        "source_time_ns":event.source_time_ns,"mapped_time_ns":event.mapped_time_ns,
        "delivery_time_ns":event.delivery_time_ns,"quality":quality_name(event.quality)
    })
}

pub fn event_json(event: &Event<HostInput, HostEvent>) -> Value {
    event_value(event, false)
}

pub(crate) fn receipt_json(value: &ObservationReceipt) -> Value {
    json!({"assertion_index":value.assertion_index,"session_id":value.session_id,
        "event_sequence":value.event_sequence,"source_node":value.source_node,
        "source_port":value.source_port,"kind":value.kind,"source_time_ns":value.source_time_ns,
        "mapped_time_ns":value.mapped_time_ns,"delivery_time_ns":value.delivery_time_ns,
        "quality":quality_name(value.quality)})
}

pub fn result_json(result: &ExperimentResult) -> Value {
    json!({"status":result.status.iter().map(|v| match v {
        Status::Pending=>"pending",Status::Pass=>"pass",Status::Fail=>"fail"
    }).collect::<Vec<_>>(),"failures":result.failures,
        "matches":result.matches.iter().map(receipt_json).collect::<Vec<_>>(),
        "end_ns":result.end_ns,"origin_ns":result.origin_ns,
        "delivered_inputs":result.delivered_inputs,"events_total":result.events_total,
        "ready_receipts":result.ready_receipts.iter().map(|(k,v)| (k.clone(),receipt_json(v))).collect::<Map<_,_>>(),
        "boards":result.boards})
}
