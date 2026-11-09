//! Compiled DM-MC02 profile: public GPIO/snapshot projection and canonical
//! UART/fixed84 CAN wires. Native execution and cleanup belong to QemuMachine.
mod can;
mod cosim;
pub use crate::boards::{digital_level, gpio_pin, validate_snapshot};
use crate::{
    can::EndpointId,
    fabric::{
        digital::{DigitalAttachment, DigitalSample},
        imu::ImuAttachment,
        uart::{UartAttachment, UartConnection, UartEndpointId, UartStamp},
    },
    launch,
    machine::{
        admission, backend_error, state_error, validate_execution, NativeBackend, ProfileAdapter,
    },
    runtime::{CanAttachment, CanConnection, HostContext, HostEvent, HostInput, HostOptions},
    transports::{fixed84, stream::StreamTransport},
};
use serde_json::{json, Value};
use sim_core::{
    BuildContext, Capability, Direction, EventTime, NodeDescription, NodeSpec, PortCategory,
    PortSpec, Result, TimeQuality, Topology,
};
use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    os::unix::net::UnixStream,
    path::PathBuf,
    rc::{Rc, Weak},
};

const UARTS: &[&str] = &["uart1", "uart2", "uart3", "uart7", "uart10"];
const CAN_PORTS: &[&str] = &["fdcan1", "fdcan2", "fdcan3"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MachineInput {
    Bool(bool),
    Level(bool),
    Bytes(Vec<u8>),
    AdcRaw(u16),
    AdcVoltage(u32),
}
fn options(node: &NodeSpec<HostOptions>) -> Result<&crate::machine::QemuOptions> {
    let HostOptions::Qemu(options) = &node.options else {
        return Err(admission("machine.qemu requires QEMU options"));
    };
    if options.firmware.as_os_str().is_empty()
        || options.firmware.as_os_str().as_encoded_bytes().contains(&0)
        || options.gdb_port == Some(0)
    {
        return Err(admission(
            "QEMU firmware path must be nonempty without NUL; GDB port requires 1..65535",
        ));
    }
    let mut seen = BTreeSet::new();
    if options
        .uarts
        .iter()
        .any(|port| !UARTS.contains(&port.as_str()) || !seen.insert(port))
    {
        return Err(admission("UART endpoints require distinct uart1/2/3/7/10"));
    }
    for path in [&options.qemu, &options.backend_root, &options.qmp_path]
        .into_iter()
        .flatten()
    {
        if path.as_os_str().is_empty() || path.as_os_str().as_encoded_bytes().contains(&0) {
            return Err(admission("QEMU paths must be nonempty without NUL"));
        }
    }
    Ok(options)
}
fn port(
    name: impl Into<String>,
    protocol: &str,
    direction: Direction,
    signal: bool,
    external: bool,
) -> PortSpec {
    let mut port = PortSpec::new(name, protocol);
    port.direction = direction;
    if signal {
        port.category = PortCategory::Signal;
    }
    if external {
        port.capabilities.insert("external_input".into());
    }
    port
}
pub fn describe(node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
    let options = options(node)?;
    let mut ports: Vec<_> = options
        .uarts
        .iter()
        .map(|name| port(name, "uart", Direction::InOut, false, true))
        .collect();
    if options.remote {
        ports.push(port("remote", "remote", Direction::In, false, true));
    }
    if options.adc {
        ports.extend((0..32).map(|channel| {
            port(
                format!("adc1.ch{channel}"),
                "adc",
                Direction::In,
                true,
                true,
            )
        }));
    }
    ports.extend(
        CAN_PORTS
            .iter()
            .map(|name| port(*name, "can", Direction::InOut, false, false)),
    );
    ports.push(port("key", "digital", Direction::In, true, true));
    ports.push(port("imu", "imu", Direction::In, true, false));
    ports.push(port("snapshot", "snapshot", Direction::Out, true, false));
    for bank in 'A'..='H' {
        for index in 0..16 {
            ports.push(port(
                format!("gpio.{bank}{index}"),
                "digital",
                Direction::In,
                true,
                true,
            ));
            ports.push(port(
                format!("gpio.{bank}{index}.out"),
                "digital",
                Direction::Out,
                true,
                false,
            ));
        }
    }
    Ok(NodeDescription::new(
        ports,
        [
            Capability::Machine,
            Capability::Input,
            Capability::InputCommit,
            Capability::Health,
        ],
    ))
}
pub fn input_identity(port: &str) -> &str {
    if port == "key" {
        "gpio.A15"
    } else {
        port.strip_suffix(".out").unwrap_or(port)
    }
}
pub fn validate_input(
    node: &NodeSpec<HostOptions>,
    port: &str,
    value: HostInput,
    connected: &[String],
) -> Result<HostInput> {
    let options = options(node)?;
    let HostInput::Machine(input) = &value else {
        return Err(admission("QEMU input requires a typed machine input"));
    };
    match (port, input) {
        ("key", MachineInput::Bool(_)) => (),
        (port, MachineInput::Level(_)) if port.starts_with("gpio.") && !port.ends_with(".out") => {
            gpio_pin(&port[5..])?;
        }
        (port, MachineInput::Bytes(bytes))
            if options.uarts.iter().any(|uart| uart == port) && bytes.len() <= 4096 => {}
        ("remote", MachineInput::Bytes(bytes)) if options.remote && bytes.len() <= 4096 => {}
        (port, MachineInput::AdcRaw(_) | MachineInput::AdcVoltage(_)) if options.adc => {
            cosim::adc_section(cosim::adc_channel(port)?, input)?;
        }
        _ => return Err(admission(
            "QEMU input requires key boolean, GPIO level or opened UART/remote bytes up to 4096",
        )),
    }
    if connected
        .iter()
        .any(|peer| input_identity(peer) == input_identity(port))
    {
        return Err(admission("QEMU physical input is owned by a connection"));
    }
    Ok(value)
}
pub fn validate_topology(
    topology: &Topology<HostOptions>,
    descriptions: &[NodeDescription],
) -> Result<()> {
    let mut gdb = BTreeSet::new();
    let mut qmp = BTreeSet::new();
    for (index, node) in topology.nodes.iter().enumerate() {
        if !matches!(node.options, HostOptions::Qemu(_)) {
            continue;
        }
        let options = options(node)?;
        if options.gdb_port.is_some_and(|port| !gdb.insert(port)) {
            return Err(admission("QEMU nodes cannot share a GDB port"));
        }
        if let Some(path) = &options.qmp_path {
            if !qmp
                .insert(launch::socket_path(Some(path)).map_err(|error| admission(error.message))?)
            {
                return Err(admission("QEMU nodes cannot share an external QMP socket"));
            }
        }
        let mut pins = BTreeSet::new();
        for link in &topology.links {
            for endpoint in [&link.source, &link.target] {
                if endpoint.node != node.name {
                    continue;
                }
                let descriptor = descriptions[index].port(&endpoint.port)?;
                if !["can", "digital", "uart", "imu"].contains(&descriptor.protocol.as_str()) {
                    return Err(admission("unsupported QEMU connection protocol"));
                }
                if descriptor.protocol == "digital" && !pins.insert(input_identity(&endpoint.port))
                {
                    return Err(admission("QEMU pin aliases share one physical GPIO pin"));
                }
            }
        }
    }
    Ok(())
}

fn lower(error: crate::Failure) -> sim_core::Failure {
    error
}

pub(crate) struct Shared {
    streams: BTreeMap<String, StreamTransport<UnixStream>>,
    snapshot: Option<Value>,
    samples: BTreeMap<String, DigitalSample>,
    baselines: BTreeMap<String, (Value, Value)>,
    goals: BTreeMap<String, u64>,
    cosim: Option<cosim::CosimEndpoint>,
}
pub struct DmMc02Adapter {
    context: HostContext,
    firmware: PathBuf,
    uarts: Vec<String>,
    remote: bool,
    adc: bool,
    imu_connected: bool,
    cans: Vec<String>,
    outputs: Vec<String>,
    state: Rc<RefCell<Shared>>,
    can_connections: BTreeMap<String, CanConnection>,
    uart_connections: BTreeMap<String, UartConnection>,
}
fn with_backend<T>(
    backend: &Weak<RefCell<NativeBackend>>,
    context: &HostContext,
    f: impl FnOnce(&mut NativeBackend) -> Result<T>,
) -> Result<T> {
    context.check_io()?;
    context.check_deadline()?;
    let owner = backend
        .upgrade()
        .ok_or_else(|| state_error("QEMU machine is no longer owned"))?;
    let mut owner = owner.try_borrow_mut().map_err(|_| {
        context.fail(state_error(
            "QEMU borrowed IO cannot reenter an active native operation",
        ))
    })?;
    f(&mut owner).map_err(|error| context.fail(error))
}
impl DmMc02Adapter {
    pub(crate) fn create(
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
        backend: Weak<RefCell<NativeBackend>>,
    ) -> Result<Self> {
        let options = options(node)?;
        let firmware = launch::readable_file(&options.firmware, "firmware ELF")
            .map_err(|error| admission(error.message))?;
        let context = build.context();
        let state = Rc::new(RefCell::new(Shared {
            streams: BTreeMap::new(),
            snapshot: None,
            samples: BTreeMap::new(),
            baselines: BTreeMap::new(),
            goals: BTreeMap::new(),
            cosim: None,
        }));
        let mut can_connections = BTreeMap::new();
        let mut uart_connections = BTreeMap::new();
        let mut outputs = vec![];
        for port in describe(node)?.ports {
            if build.peers(&port.name)?.is_empty() {
                continue;
            }
            let name = port.name.clone();
            let weak = Rc::downgrade(&state);
            let backend = backend.clone();
            let context = context.clone();
            match port.protocol.as_str() {
                "imu" => {
                    build.attach::<_, ()>(
                        &name,
                        ImuAttachment::Sink(Rc::new(move |sample| {
                            with_backend(&backend, &context, |backend| {
                                if backend.pending().is_some() {
                                    return Err(state_error(
                                        "IMU input requires a completed paused cut",
                                    ));
                                }
                                let (execution, status) = backend.query_execution()?;
                                validate_execution(
                                    &execution,
                                    &status,
                                    backend.to_native(backend.t_sim_ns())?,
                                    backend.t_sim_ns() == context.session()?.t_sim_ns,
                                )?;
                                let state = weak.upgrade().ok_or_else(|| {
                                    state_error("QEMU profile is no longer owned")
                                })?;
                                state
                                    .borrow_mut()
                                    .cosim
                                    .as_mut()
                                    .ok_or_else(|| {
                                        state_error("QEMU IMU endpoint has not started")
                                    })?
                                    .stage_imu(sample, backend.t_sim_ns())?;
                                backend.check()
                            })
                        })),
                    )?;
                }
                "can" => {
                    let name_cb = name.clone();
                    let connection = build.attach(
                        &name,
                        CanAttachment {
                            endpoint: EndpointId::new(),
                            receive: Rc::new(move |frame, _| {
                                with_backend(&backend, &context, |backend| {
                                    if backend.pending().is_some() {
                                        return Err(state_error(
                                            "CAN input requires a completed paused cut",
                                        ));
                                    }
                                    let state = weak.upgrade().ok_or_else(|| {
                                        state_error("QEMU profile is no longer owned")
                                    })?;
                                    let mut state = state.borrow_mut();
                                    let target = backend.to_native(context.t_sim_ns()?)?;
                                    let initial =
                                        context.t_sim_ns()? == context.session()?.t_sim_ns;
                                    let (execution, status) =
                                        can::execution(backend, std::slice::from_ref(&name_cb))?;
                                    validate_execution(&execution, &status, target, initial)?;
                                    let baseline =
                                        state.baselines.get(&name_cb).ok_or_else(|| {
                                            state_error("CAN native baseline not established")
                                        })?;
                                    let count = state.goals.get(&name_cb).copied().unwrap_or(
                                        baseline.1["completed_frames"].as_u64().unwrap(),
                                    );
                                    let goal = count
                                        .checked_add(1)
                                        .filter(|goal| *goal < u64::MAX)
                                        .ok_or_else(|| {
                                            state_error("CAN input receipt counter exhausted")
                                        })?;
                                    state.goals.insert(name_cb.clone(), goal);
                                    state
                                        .streams
                                        .get_mut(&can::wire(&name_cb))
                                        .unwrap()
                                        .send(
                                            fixed84::encode(&frame.with_timestamp(target)).to_vec(),
                                        )
                                        .map_err(lower)?;
                                    backend.check()
                                })
                            }),
                        },
                    )?;
                    can_connections.insert(name, connection);
                }
                "uart" => {
                    let name_cb = name.clone();
                    let connection = build.attach(
                        &name,
                        UartAttachment {
                            endpoint: UartEndpointId::new(),
                            receive: Rc::new(move |bytes, _| {
                                with_backend(&backend, &context, |backend| {
                                    if backend.pending().is_some() {
                                        return Err(state_error(
                                            "UART input requires a completed paused cut",
                                        ));
                                    }
                                    let (execution, status) = backend.query_execution()?;
                                    validate_execution(
                                        &execution,
                                        &status,
                                        backend.to_native(backend.t_sim_ns())?,
                                        backend.t_sim_ns() == context.session()?.t_sim_ns,
                                    )?;
                                    let state = weak.upgrade().ok_or_else(|| {
                                        state_error("QEMU profile is no longer owned")
                                    })?;
                                    if !bytes.is_empty() {
                                        state
                                            .borrow_mut()
                                            .streams
                                            .get_mut(&name_cb)
                                            .unwrap()
                                            .send(bytes.to_vec())
                                            .map_err(lower)?;
                                    }
                                    backend.check()
                                })
                            }),
                        },
                    )?;
                    uart_connections.insert(name, connection);
                }
                "digital" if port.direction == Direction::Out => {
                    outputs.push(name.clone());
                    let name_cb = name.clone();
                    build.attach::<_, ()>(
                        &name,
                        DigitalAttachment::Source(Rc::new(move || {
                            context.check_io()?;
                            context.check_deadline()?;
                            let owner = backend
                                .upgrade()
                                .ok_or_else(|| state_error("QEMU machine is no longer owned"))?;
                            if owner
                                .try_borrow()
                                .map_err(|_| {
                                    state_error("digital sample requires completed QEMU operation")
                                })?
                                .pending()
                                .is_some()
                            {
                                return Err(state_error(
                                    "digital sample requires a completed QEMU target",
                                ));
                            }
                            let state = weak
                                .upgrade()
                                .ok_or_else(|| state_error("QEMU profile is no longer owned"))?;
                            let sample =
                                state
                                    .borrow()
                                    .samples
                                    .get(&name_cb)
                                    .copied()
                                    .ok_or_else(|| {
                                        state_error("digital source has no completed sample")
                                    })?;
                            Ok(sample)
                        })),
                    )?;
                }
                "digital" => {
                    let name_cb = name.clone();
                    build.attach::<_, ()>(
                        &name,
                        DigitalAttachment::Sink(Rc::new(move |level| {
                            with_backend(&backend, &context, |backend| {
                                if backend.pending().is_some() {
                                    return Err(state_error(
                                        "digital input requires a completed paused cut",
                                    ));
                                }
                                let (execution, status) = backend.query_execution()?;
                                validate_execution(
                                    &execution,
                                    &status,
                                    backend.to_native(backend.t_sim_ns())?,
                                    backend.t_sim_ns() == context.session()?.t_sim_ns,
                                )?;
                                set_digital(backend, &name_cb, level, true)
                            })
                        })),
                    )?;
                }
                _ => return Err(admission("unsupported QEMU attachment")),
            }
        }
        Ok(Self {
            context,
            firmware,
            uarts: options.uarts.clone(),
            remote: options.remote,
            adc: options.adc,
            imu_connected: !build.peers("imu")?.is_empty(),
            cans: can_connections.keys().cloned().collect(),
            outputs,
            state,
            can_connections,
            uart_connections,
        })
    }
    fn update_snapshot(&self, value: Value, cut: u64, target: u64) -> Result<()> {
        validate_snapshot(&value)?;
        if value["timestamp_ns"] != target {
            return Err(state_error(
                "native snapshot moved outside the completed target",
            ));
        }
        let mut samples = BTreeMap::new();
        for output in &self.outputs {
            let endpoint = output.strip_suffix(".out").unwrap();
            let (bank, pin) = gpio_pin(&endpoint[5..])?;
            let driven = value["gpio"][bank]["output_mask"].as_u64().unwrap() & (1 << pin) != 0;
            samples.insert(
                output.clone(),
                DigitalSample {
                    level: if driven {
                        Some(digital_level(endpoint, &value)?)
                    } else {
                        None
                    },
                    source_time_ns: target,
                    mapped_time_ns: cut,
                },
            );
        }
        let mut state = self.state.borrow_mut();
        state.snapshot = Some(value);
        state.samples = samples;
        Ok(())
    }
    fn emit_snapshot(&self, cut: u64, target: u64) -> Result<()> {
        let value = self.state.borrow().snapshot.clone().unwrap();
        self.context.emit(
            Some("snapshot"),
            "snapshot",
            HostEvent::Snapshot { value },
            exact(target, cut),
        )?;
        Ok(())
    }
    fn drain_can(
        &mut self,
        backend: &mut NativeBackend,
        port: &str,
        cut: u64,
        target: u64,
    ) -> Result<()> {
        let baseline = self.state.borrow().baselines[port].0["tx_dropped"].clone();
        loop {
            let (tx, _) = can::observe(backend, port, target, false)?;
            if tx["tx_dropped"] != baseline {
                return Err(state_error("CAN output drop count changed"));
            }
            let frames = {
                let mut state = self.state.borrow_mut();
                let stream = state.streams.get_mut(&can::wire(port)).unwrap();
                backend.check()?;
                stream.read_once().map_err(lower)?;
                backend.check()?;
                let mut frames = vec![];
                while let Some(bytes) = stream.next_record(fixed84::WIRE_SIZE).map_err(lower)? {
                    frames.push(fixed84::decode(&bytes).map_err(lower)?);
                }
                frames
            };
            let empty = frames.is_empty();
            for frame in frames {
                let mapped = backend.to_sim(frame.timestamp_ns())?;
                if mapped > cut {
                    return Err(state_error("QEMU emitted CAN beyond its completed target"));
                }
                self.can_connections
                    .get_mut(port)
                    .unwrap()
                    .send(frame.with_timestamp(mapped))?;
                self.context.emit(
                    Some(port),
                    "can-tx",
                    HostEvent::MachineCanTx {
                        frame: frame.clone(),
                    },
                    EventTime {
                        source_time_ns: Some(frame.timestamp_ns()),
                        mapped_time_ns: Some(mapped),
                        delivery_time_ns: Some(cut),
                        quality: Some(TimeQuality::Exact),
                    },
                )?;
                backend.check()?;
            }
            if tx["pending_frames"] == 0
                && empty
                && self.state.borrow().streams[&can::wire(port)].buffered_bytes() == 0
            {
                let (final_tx, _) = can::observe(backend, port, target, false)?;
                if final_tx["pending_frames"] != 0 || final_tx["tx_dropped"] != baseline {
                    return Err(state_error("CAN producer changed during drain"));
                }
                return backend.check();
            }
        }
    }
}
fn exact(source: u64, cut: u64) -> EventTime {
    EventTime {
        source_time_ns: Some(source),
        mapped_time_ns: Some(cut),
        delivery_time_ns: Some(cut),
        quality: Some(TimeQuality::Exact),
    }
}
fn set_digital(backend: &mut NativeBackend, port: &str, level: bool, physical: bool) -> Result<()> {
    if port == "key" {
        backend.set_property("user-key", json!(if physical { !level } else { level }))
    } else {
        let pin = port
            .strip_prefix("gpio.")
            .ok_or_else(|| admission("unknown digital input"))?;
        gpio_pin(pin)?;
        backend.set_property("gpio-input", json!(format!("{pin}={}", u8::from(level))))
    }
}
impl ProfileAdapter for DmMc02Adapter {
    fn streams(&self) -> Vec<String> {
        self.uarts
            .iter()
            .cloned()
            .chain(self.remote.then_some("uart5".into()))
            .chain((self.adc || self.imu_connected).then_some("cosim".into()))
            .chain(self.cans.iter().map(|port| can::wire(port)))
            .collect()
    }
    fn machine_arguments(&self) -> Vec<OsString> {
        crate::boards::launch::machine_arguments(&self.firmware)
    }
    fn serial_slots(&self) -> &'static [&'static str] {
        crate::boards::launch::SERIAL_SLOTS
    }
    fn start(&mut self, backend: &mut NativeBackend) -> Result<()> {
        for port in self.streams() {
            let stream = backend.connect_stream(&port)?;
            self.state
                .borrow_mut()
                .streams
                .insert(port, StreamTransport::new(stream, 256).map_err(lower)?);
        }
        let properties = backend.command(crate::qmp::commands::qom_list {
            path: "/machine".into(),
        })?;
        for required in std::iter::once("board-state")
            .chain((!self.cans.is_empty()).then_some("can-execution-state"))
        {
            if !properties.iter().any(|property| property.name == required) {
                return Err(backend_error(format!("QEMU lacks {required}")));
            }
        }
        let target = backend.native_origin_ns()?;
        self.update_snapshot(backend.property("board-state")?, backend.t_sim_ns(), target)?;
        if self.adc || self.imu_connected {
            let mut state = self.state.borrow_mut();
            let endpoint = cosim::CosimEndpoint::start(
                self.context.clone(),
                backend,
                state.streams.get_mut("cosim").unwrap(),
                self.imu_connected,
                self.adc,
            )?;
            state.cosim = Some(endpoint);
        }
        for port in &self.cans {
            let baseline = can::initial(backend, &self.state.borrow(), port)?;
            self.state
                .borrow_mut()
                .baselines
                .insert(port.clone(), baseline);
        }
        self.emit_snapshot(backend.t_sim_ns(), target)
    }
    fn execution(&mut self, backend: &mut NativeBackend) -> Result<(Value, Value)> {
        if self.cans.is_empty() {
            backend.query_execution()
        } else {
            can::execution(backend, &self.cans)
        }
    }
    fn service(&mut self, backend: &mut NativeBackend, cut: u64) -> Result<()> {
        let mut state = self.state.borrow_mut();
        let Shared { cosim, streams, .. } = &mut *state;
        if let Some(cosim) = cosim {
            cosim.service(backend, streams.get_mut("cosim").unwrap(), cut)?;
        }
        Ok(())
    }
    fn complete_cut(&mut self, backend: &mut NativeBackend, cut: u64, target: u64) -> Result<()> {
        if self.remote {
            backend.check()?;
            self.state
                .borrow_mut()
                .streams
                .get_mut("uart5")
                .unwrap()
                .flush()
                .map_err(lower)?;
            backend.check()?;
        }
        for port in self.cans.clone() {
            self.drain_can(backend, &port, cut, target)?;
        }
        let mut batches = vec![];
        for port in &self.uarts {
            backend.check()?;
            let mut state = self.state.borrow_mut();
            let stream = state.streams.get_mut(port).unwrap();
            stream.flush().map_err(lower)?;
            stream.read_once().map_err(lower)?;
            let length = stream.buffered_bytes();
            if length > 0 {
                batches.push((
                    port.clone(),
                    stream.next_record(length).map_err(lower)?.unwrap(),
                ));
            }
            drop(state);
            backend.check()?;
        }
        // A fresh execution observation follows every UART read. The completed
        // paused target is therefore a real upper bound for these host chunks.
        let (execution, status) = self.execution(backend)?;
        validate_execution(&execution, &status, target, false)?;
        self.update_snapshot(backend.property("board-state")?, cut, target)?;
        for (port, data) in batches {
            if let Some(connection) = self.uart_connections.get_mut(&port) {
                connection.send(
                    &data,
                    UartStamp {
                        source_time_ns: Some(target),
                        mapped_time_ns: Some(cut),
                        quality: TimeQuality::UpperBound,
                    },
                )?;
            }
            let mut time = exact(target, cut);
            time.quality = Some(TimeQuality::UpperBound);
            self.context
                .emit(Some(&port), "uart", HostEvent::MachineUart { data }, time)?;
            backend.check()?;
        }
        self.emit_snapshot(cut, target)
    }
    fn input(&mut self, backend: &mut NativeBackend, port: &str, value: HostInput) -> Result<()> {
        let HostInput::Machine(input) = value else {
            return Err(admission("QEMU input requires a typed machine value"));
        };
        match input {
            input @ (MachineInput::AdcRaw(_) | MachineInput::AdcVoltage(_)) if self.adc => {
                self.state.borrow_mut().cosim.as_mut().unwrap().stage_adc(
                    cosim::adc_channel(port)?,
                    &input,
                    backend.t_sim_ns(),
                )
            }
            MachineInput::Bool(value) if port == "key" => set_digital(backend, port, value, false),
            MachineInput::Level(value) => set_digital(backend, port, value, false),
            MachineInput::Bytes(bytes)
                if (self.uarts.iter().any(|uart| uart == port)
                    || port == "remote" && self.remote)
                    && bytes.len() <= 4096 =>
            {
                let wire = if port == "remote" { "uart5" } else { port };
                if !bytes.is_empty() {
                    self.state
                        .borrow_mut()
                        .streams
                        .get_mut(wire)
                        .unwrap()
                        .send(bytes)
                        .map_err(lower)?;
                }
                backend.check()
            }
            _ => Err(admission("unsupported QEMU input")),
        }
    }
    fn commit_inputs(&mut self, backend: &mut NativeBackend, cut: u64) -> Result<()> {
        {
            let mut state = self.state.borrow_mut();
            let Shared { cosim, streams, .. } = &mut *state;
            if let Some(cosim) = cosim {
                cosim.commit(backend, streams.get_mut("cosim").unwrap(), cut)?;
            }
        }
        let goals = self.state.borrow().goals.clone();
        for (port, goal) in goals {
            let receipt = can::receipt(backend, &mut self.state.borrow_mut(), &port, goal, cut)?;
            {
                let mut state = self.state.borrow_mut();
                state.baselines.get_mut(&port).unwrap().1 = receipt.clone();
                state.goals.remove(&port);
            }
            self.context.emit(
                Some(&port),
                "can-input-processed",
                HostEvent::CanInputProcessed { value: receipt },
                exact(backend.to_native(cut)?, cut),
            )?;
            backend.check()?;
        }
        Ok(())
    }
    fn observe(&self) -> Result<Value> {
        self.state
            .borrow()
            .snapshot
            .clone()
            .ok_or_else(|| state_error("QEMU has no completed snapshot"))
    }
    fn close(&mut self) -> Result<()> {
        let mut state = self.state.borrow_mut();
        if let Some(cosim) = &mut state.cosim {
            cosim.close();
        }
        // Drop each owned socket independently in reverse acquisition order.
        for port in self.streams().into_iter().rev() {
            state.streams.remove(&port);
        }
        Ok(())
    }
}
