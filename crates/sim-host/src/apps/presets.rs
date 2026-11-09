//! Data-only four-wheel wiring, consumed by the ordinary sim/v1 parser.
use serde_json::{json, Map, Value};
use sim_core::{Error, Failure, Result};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
    sync::Arc,
};

const CORNERS: [&str; 4] = ["fl", "fr", "rl", "rr"];

pub struct FourWheelOptions {
    /// One or two machines; order selects the alpha/ and bravo/ model prefixes.
    pub firmwares: Vec<(String, PathBuf)>,
    pub model: PathBuf,
    pub ready: BTreeMap<String, String>,
    pub referees: BTreeMap<String, Map<String, Value>>,
    pub duration_ns: u64,
    pub quantum_ns: u64,
    pub imus: bool,
    pub inputs: Vec<Value>,
    pub assertions: Vec<Value>,
}

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn absolute(path: &std::path::Path) -> Result<PathBuf> {
    std::path::absolute(path).map_err(|e| Arc::new(e) as Failure)
}

pub fn four_wheel_document(options: FourWheelOptions) -> Result<Value> {
    if !(1..=2).contains(&options.firmwares.len()) {
        return Err(admission(
            "the fixed chassis preset requires one or two named firmwares",
        ));
    }
    let names: BTreeSet<_> = options
        .firmwares
        .iter()
        .map(|(name, _)| name.as_str())
        .collect();
    if names.len() != options.firmwares.len()
        || options
            .ready
            .keys()
            .chain(options.referees.keys())
            .any(|name| !names.contains(name.as_str()))
    {
        return Err(admission(
            "ready/referee configurations must name a unique declared machine",
        ));
    }
    let mut nodes = Map::new();
    let mut links = Vec::new();
    let mut joints = Map::new();
    let mut sensors = Map::new();
    let mut declarations = Vec::new();
    let mut inputs = options.inputs;
    let count = options.firmwares.len();
    for (index, (name, firmware)) in options.firmwares.into_iter().enumerate() {
        let prefix = if count == 2 {
            ["alpha/", "bravo/"][index]
        } else {
            ""
        };
        let has_referee = options.referees.contains_key(&name);
        nodes.insert(name.clone(), json!({"kind":"machine.qemu", "firmware":absolute(&firmware)?, "uarts":if has_referee { vec!["uart1","uart7"] } else { vec!["uart1"] }}));
        let bus = format!("{name}_can");
        nodes.insert(bus.clone(), json!({"kind":"bus.can"}));
        links.push(json!([format!("{name}.fdcan1"), format!("{bus}.can")]));
        for (id, corner) in CORNERS.into_iter().enumerate() {
            let motor = format!("{name}_motor_{corner}");
            let joint = format!("{prefix}wheel_{corner}");
            joints.insert(joint.clone(), json!(format!("{prefix}drive_{corner}")));
            nodes.insert(
                motor.clone(),
                json!({"kind":"device.dji-m3508","motor_id":id+1,"plant":"joint","joint":joint}),
            );
            links.extend([
                json!([format!("{motor}.can"), format!("{bus}.can")]),
                json!([format!("{motor}.joint"), "world.drives"]),
            ]);
        }
        if options.imus {
            sensors.insert(
                name.clone(),
                json!([format!("{prefix}imu_gyro"), format!("{prefix}imu_accel")]),
            );
            let net = format!("{name}_imu");
            nodes.insert(net.clone(), json!({"kind":"net.imu"}));
            links.extend([
                json!([format!("world.imu.{name}"), format!("{net}.signal")]),
                json!([format!("{name}.imu"), format!("{net}.signal")]),
            ]);
        }
        if let Some(text) = options.ready.get(&name) {
            declarations.push(json!({"name":name,"endpoint":format!("{name}.uart1"),"text":text}));
        }
        if let Some(configuration) = options.referees.get(&name) {
            let referee = format!("{name}_referee");
            let serial = format!("{name}_referee_uart");
            let mut configuration = configuration.clone();
            configuration.insert("kind".into(), json!("device.referee"));
            nodes.insert(referee.clone(), Value::Object(configuration));
            nodes.insert(serial.clone(), json!({"kind":"net.uart"}));
            links.extend([
                json!([format!("{referee}.uart"), format!("{serial}.uart")]),
                json!([format!("{name}.uart7"), format!("{serial}.uart")]),
            ]);
            inputs.push(json!({"at":"0ns","endpoint":format!("{referee}.enable"),"value":true,"origin":if options.ready.contains_key(&name) {format!("marker:{name}")} else {"session".into()}}));
        }
    }
    nodes.insert("world".into(),json!({"kind":"physics.mujoco","model":absolute(&options.model)?,"control":"bound","joints":joints,"imus":sensors}));
    Ok(
        json!({"schema":"sim/v1","runtime":{"quantum":format!("{}ns",options.quantum_ns),"duration":format!("{}ns",options.duration_ns)},"nodes":nodes,"links":links,"experiment":{"ready":declarations,"inputs":inputs,"assertions":options.assertions}}),
    )
}

pub enum CanFault {
    Delay(u64),
    Feedback { motor_id: u8, enabled: bool },
}

/// Delays precede feedback changes at equal times; caller order otherwise stays.
pub fn add_can_faults(
    document: &mut Value,
    member: &str,
    mut changes: Vec<(u64, CanFault)>,
) -> Result<()> {
    let bus = format!("{member}_can");
    if document
        .get("nodes")
        .and_then(|nodes| nodes.get(&bus))
        .and_then(Value::as_object)
        .is_none()
    {
        return Err(admission("CAN fault member is not in this scene"));
    }
    if document
        .pointer("/experiment/inputs")
        .and_then(Value::as_array)
        .is_none()
    {
        return Err(admission("CAN faults require an experiment input list"));
    }
    changes
        .sort_by_key(|(at, action)| (*at, u8::from(matches!(action, CanFault::Feedback { .. }))));
    let mut profiles = Map::new();
    let mut inputs = Vec::new();
    let mut delay = 0;
    let mut blocked = BTreeSet::new();
    for (at, action) in changes {
        match action {
            CanFault::Delay(value) => delay = value,
            CanFault::Feedback { motor_id, enabled } => {
                if !(1..=4).contains(&motor_id) {
                    return Err(admission("feedback fault requires motor ID1..4"));
                }
                if enabled {
                    blocked.remove(&motor_id);
                } else {
                    blocked.insert(motor_id);
                }
            }
        }
        let drops: Vec<_> = blocked.iter().map(|id| json!({"source":format!("{member}_motor_{}.can",CORNERS[*id as usize-1]),"destination":format!("{member}.fdcan1"),"can_id":0x200+u16::from(*id)})).collect();
        let profile = json!({"delay_ns":delay,"drop":drops});
        let name = if let Some((name, _)) = profiles.iter().find(|(_, value)| **value == profile) {
            name.clone()
        } else {
            if profiles.len() >= 16 {
                return Err(admission(
                    "scene requires more than16 distinct CAN fault profiles",
                ));
            }
            let name = format!("policy{}", profiles.len());
            profiles.insert(name.clone(), profile);
            name
        };
        inputs.push(json!({"at":format!("{at}ns"),"endpoint":format!("{bus}.fault"),"value":name,"origin":"session"}));
    }
    if !profiles.is_empty() {
        document["nodes"][&bus]["fault_profiles"] = Value::Object(profiles);
    }
    document["experiment"]["inputs"]
        .as_array_mut()
        .expect("admitted inputs")
        .extend(inputs);
    Ok(())
}
