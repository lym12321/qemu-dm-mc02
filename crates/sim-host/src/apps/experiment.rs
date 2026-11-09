//! Finite observation matching driven by the sole Simulation scheduler.

use crate::{
    assertions::{Evaluator, Observation, ObservationData, Status},
    config::scene::{ExperimentAssertion, ExperimentInput, Origin, ReadyMarker, Scene},
    runtime::{self, HostContext, HostEvent, HostInput, HostOptions},
};
use sim_core::{
    BuildContext, Capability, Component, Event, EventPayload, Factory, Failure, NodeDescription,
    NodeSpec, Observer, Result, Sampler, Simulation, SimulationOptions, StepStatus, TimeQuality,
    Topology,
};
use std::{
    cell::{Cell, RefCell},
    collections::{BTreeMap, BTreeSet},
    fmt,
    rc::Rc,
    sync::Arc,
    time::{Duration, Instant},
};

#[derive(Clone, Debug)]
pub struct ObservationReceipt {
    pub assertion_index: Option<usize>,
    pub session_id: u64,
    pub event_sequence: u64,
    pub source_node: String,
    pub source_port: Option<String>,
    pub kind: String,
    pub source_time_ns: Option<u64>,
    pub mapped_time_ns: u64,
    pub delivery_time_ns: u64,
    pub quality: TimeQuality,
}

#[derive(Debug)]
pub struct ExperimentResult {
    pub status: Vec<Status>,
    pub failures: Vec<String>,
    pub matches: Vec<ObservationReceipt>,
    pub end_ns: u64,
    pub origin_ns: u64,
    pub delivered_inputs: u64,
    pub events_total: u64,
    pub ready_receipts: BTreeMap<String, ObservationReceipt>,
    pub boards: BTreeMap<String, serde_json::Value>,
}
impl ExperimentResult {
    pub fn passed(&self) -> bool {
        self.failures.is_empty() && self.status.iter().all(|v| *v == Status::Pass)
    }
}

#[derive(Debug)]
pub struct RunError {
    pub primary: Failure,
    pub notes: Vec<String>,
    pub time_ns: u64,
    pub events_total: u64,
}
impl fmt::Display for RunError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.primary.fmt(formatter)
    }
}
impl std::error::Error for RunError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.primary.as_ref())
    }
}
pub(crate) fn admission(message: &str) -> Failure {
    Arc::new(sim_core::Error::Admission(message.into()))
}
fn lower(error: crate::Failure) -> Failure {
    error
}

pub(crate) enum Observed<'a> {
    None,
    Can(&'a crate::can::CanFrame, bool),
    Uart(&'a [u8], Option<&'a str>),
    Digital(Option<bool>),
    #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
    Snapshot(&'a serde_json::Value),
}
struct Group {
    ready: bool,
    indices: Vec<usize>,
    evaluator: Evaluator,
}
pub(crate) struct Consumer {
    assertions: Vec<ExperimentAssertion>,
    duration_ns: u64,
    groups: Vec<Group>,
    ready: Vec<ReadyMarker>,
    tails: Vec<Vec<u8>>,
    pub(crate) origin_ns: Option<u64>,
    ready_receipts: BTreeMap<String, ObservationReceipt>,
    boards: BTreeMap<String, serde_json::Value>,
    observations: BTreeMap<String, String>,
    snapshots: BTreeMap<String, Vec<(String, String)>>,
    #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
    machine_nodes: BTreeSet<String>,
    cut: Option<u64>,
    next_sequence: u64,
    delivered_inputs: u64,
    matches: Vec<ObservationReceipt>,
}
impl Consumer {
    pub(crate) fn new(
        assertions: Vec<ExperimentAssertion>,
        duration_ns: u64,
        ready: Vec<ReadyMarker>,
        descriptions: &BTreeMap<String, NodeDescription>,
        connected: &BTreeSet<String>,
    ) -> Result<Self> {
        if duration_ns == 0 || assertions.len() > 1024 || ready.len() > 64 {
            return Err(admission(
                "finite experiment requires positive duration and bounded declarations",
            ));
        }
        let mut owner = Self {
            assertions,
            duration_ns,
            groups: vec![],
            tails: vec![vec![]; ready.len()],
            origin_ns: if ready.is_empty() { Some(0) } else { None },
            ready,
            ready_receipts: BTreeMap::new(),
            boards: BTreeMap::new(),
            observations: BTreeMap::new(),
            snapshots: BTreeMap::new(),
            #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
            machine_nodes: descriptions
                .iter()
                .filter(|(_, d)| d.has(Capability::Machine))
                .map(|(n, _)| n.clone())
                .collect(),
            cut: None,
            next_sequence: 0,
            delivered_inputs: 0,
            matches: vec![],
        };
        let mut names = BTreeSet::new();
        let mut endpoints = Vec::new();
        for item in &owner.ready {
            sim_core::Endpoint::new(&item.name, "_ready")?;
            if !names.insert(&item.name) || item.text.is_empty() || item.text.len() > 4096 {
                return Err(admission("ready requires unique names and 1..4096 bytes"));
            }
            endpoints.push(("uart_contains", item.endpoint.to_string()));
        }
        for item in &owner.assertions {
            item.assertion.validate().map_err(lower)?;
            if item.assertion.before_ns > duration_ns || matches!(item.origin, Origin::Marker(_)) {
                return Err(admission(
                    "assertion requires session/ready origin and a deadline within duration",
                ));
            }
            endpoints.push((item.assertion.kind(), item.assertion.endpoint.clone()));
        }
        for (kind, endpoint) in endpoints {
            owner.admit_endpoint(kind, &endpoint, descriptions, connected)?;
        }
        owner.start_group(false, 0)?;
        if owner.ready.is_empty() {
            owner.start_group(true, 0)?;
        }
        Ok(owner)
    }
    fn admit_endpoint(
        &mut self,
        kind: &str,
        text: &str,
        descriptions: &BTreeMap<String, NodeDescription>,
        connected: &BTreeSet<String>,
    ) -> Result<()> {
        let endpoint = sim_core::Endpoint::parse(text)?;
        let description = descriptions
            .get(&endpoint.node)
            .ok_or_else(|| admission("unknown assertion node"))?;
        if kind == "digital_equals" {
            let local = endpoint.port.strip_suffix(".out").unwrap_or(&endpoint.port);
            if (local == "key" || local == "led" || local.starts_with("gpio."))
                && description.ports.iter().any(|port| {
                    port.protocol == "snapshot" && port.direction == sim_core::Direction::Out
                })
            {
                if let Some(pin) = local.strip_prefix("gpio.") {
                    crate::boards::gpio_pin(pin)?;
                }
                self.snapshots
                    .entry(endpoint.node)
                    .or_default()
                    .push((text.to_owned(), local.to_owned()));
                return Ok(());
            }
        }
        let port = description.port(&endpoint.port)?;
        let protocol = match kind {
            "can_seen" => "can",
            "uart_contains" => "uart",
            "digital_equals" => "digital",
            _ => return Err(admission("unknown assertion kind")),
        };
        if port.protocol != protocol
            || port.direction == sim_core::Direction::In
            || matches!(protocol, "can" | "uart") && description.has(Capability::Connection)
        {
            return Err(admission(
                "assertion endpoint has no matching public observations",
            ));
        }
        let indirect = connected.contains(text) && !description.has(Capability::Machine);
        let kind = match (protocol, indirect) {
            ("can", true) => "can.delivery",
            ("can", false) => "can-tx",
            ("uart", true) => "net.uart.delivery",
            ("uart", false) => "uart",
            _ => "digital",
        };
        self.observations.insert(text.to_owned(), kind.to_owned());
        Ok(())
    }
    fn start_group(&mut self, ready: bool, origin: u64) -> Result<()> {
        let indices = self
            .assertions
            .iter()
            .enumerate()
            .filter(|(_, v)| matches!(v.origin, Origin::Ready) == ready)
            .map(|(i, _)| i)
            .collect::<Vec<_>>();
        let assertions = indices
            .iter()
            .map(|i| self.assertions[*i].assertion.clone())
            .collect();
        self.groups.push(Group {
            ready,
            indices,
            evaluator: Evaluator::new(assertions, self.duration_ns, origin).map_err(lower)?,
        });
        Ok(())
    }
    fn status(&self) -> Vec<Status> {
        let mut result = vec![Status::Pending; self.assertions.len()];
        for group in &self.groups {
            for (index, status) in group.indices.iter().zip(group.evaluator.status()) {
                result[*index] = *status;
            }
        }
        result
    }
    pub(crate) fn finish(&mut self, end: u64) -> Result<()> {
        if self.origin_ns.is_none() {
            return Err(admission("experiment lacks its completed ready lifecycle"));
        }
        for group in &mut self.groups {
            group.evaluator.finish(end).map_err(lower)?;
        }
        Ok(())
    }
    pub(crate) fn result(&self, end_ns: u64, events_total: u64) -> ExperimentResult {
        let status = self.status();
        let failures = self
            .assertions
            .iter()
            .zip(&status)
            .enumerate()
            .filter(|(_, (_, s))| **s == Status::Fail)
            .map(|(i, (v, _))| {
                format!(
                    "assertion[{}] {} {} did not match before {} ns",
                    i + 1,
                    v.assertion.kind(),
                    v.assertion.endpoint,
                    v.assertion.before_ns
                )
            })
            .collect();
        ExperimentResult {
            status,
            failures,
            matches: self.matches.clone(),
            end_ns,
            origin_ns: self.origin_ns.unwrap_or(0),
            delivered_inputs: self.delivered_inputs,
            events_total,
            ready_receipts: self.ready_receipts.clone(),
            boards: self.boards.clone(),
        }
    }
    pub(crate) fn begin_cut(&mut self, cut: u64) -> Result<()> {
        if let Some(previous) = self.cut {
            if cut < previous {
                return Err(admission("experiment cuts moved backwards"));
            }
            if cut > previous {
                for group in &mut self.groups {
                    if previous >= group.evaluator.origin_ns() {
                        group.evaluator.advance(previous).map_err(lower)?;
                    }
                }
            }
        }
        self.cut = Some(cut);
        Ok(())
    }
    pub(crate) fn event(&mut self, event: &Event<HostInput, HostEvent>) -> Result<()> {
        let value = match &event.payload {
            EventPayload::Observation(HostEvent::CanDelivery { frame, dropped, .. }) => {
                Observed::Can(frame, *dropped)
            }
            EventPayload::Observation(HostEvent::UartDelivery { source, data, .. }) => {
                Observed::Uart(data, Some(source))
            }
            EventPayload::Observation(HostEvent::DigitalSample { level, .. }) => {
                Observed::Digital(*level)
            }
            #[cfg(all(target_os = "linux", feature = "machine"))]
            EventPayload::Observation(HostEvent::MachineCanTx { frame }) => {
                Observed::Can(frame, false)
            }
            #[cfg(all(target_os = "linux", feature = "machine"))]
            EventPayload::Observation(HostEvent::MachineUart { data }) => {
                Observed::Uart(data, None)
            }
            #[cfg(all(target_os = "linux", feature = "machine"))]
            EventPayload::Observation(HostEvent::Snapshot { value }) => Observed::Snapshot(value),
            _ => Observed::None,
        };
        self.observe(event, value)
    }
    fn receipt<T>(
        event: &Event<HostInput, T>,
        assertion_index: Option<usize>,
    ) -> ObservationReceipt {
        ObservationReceipt {
            assertion_index,
            session_id: event.session_id,
            event_sequence: event.sequence,
            source_node: event.source_node.clone(),
            source_port: event.source_port.clone(),
            kind: event.kind.clone(),
            source_time_ns: event.source_time_ns,
            mapped_time_ns: event.mapped_time_ns,
            delivery_time_ns: event.delivery_time_ns,
            quality: event.quality,
        }
    }
    pub(crate) fn observe<T>(
        &mut self,
        event: &Event<HostInput, T>,
        value: Observed<'_>,
    ) -> Result<()> {
        if event.session_id != 1 || event.sequence != self.next_sequence {
            return Err(admission(
                "experiment requires ordered events from its single session",
            ));
        }
        self.begin_cut(event.delivery_time_ns)?;
        self.next_sequence = self
            .next_sequence
            .checked_add(1)
            .ok_or_else(|| admission("experiment event sequence overflow"))?;
        if event.kind == "input.delivered" {
            self.delivered_inputs = self
                .delivered_inputs
                .checked_add(1)
                .ok_or_else(|| admission("experiment input count overflow"))?;
        }
        let own_endpoint = event
            .source_port
            .as_ref()
            .map(|port| format!("{}.{}", event.source_node, port));
        let endpoint = match &value {
            Observed::Uart(_, Some(source)) => Some(*source),
            _ => own_endpoint.as_deref(),
        };
        #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
        if let Observed::Snapshot(snapshot) = &value {
            self.board(event, snapshot)?;
        }
        if let Observed::Uart(data, _) = &value {
            self.observe_ready(event, endpoint, data)?;
        }
        let mut digital = Vec::new();
        let mut observed = Vec::new();
        match value {
            Observed::Can(frame, false)
                if endpoint.is_some_and(|e| {
                    self.observations
                        .get(e)
                        .is_some_and(|kind| kind == &event.kind)
                }) =>
            {
                if event.quality == TimeQuality::LowerBound {
                    return Err(admission(
                        "CAN assertion requires an observation upper bound",
                    ));
                }
                observed.push(Observation {
                    endpoint: endpoint.unwrap(),
                    timestamp_ns: event.mapped_time_ns,
                    data: ObservationData::Can(frame),
                });
            }
            Observed::Uart(data, _)
                if endpoint.is_some_and(|e| {
                    self.observations
                        .get(e)
                        .is_some_and(|kind| kind == &event.kind)
                }) =>
            {
                if event.quality == TimeQuality::LowerBound {
                    return Err(admission(
                        "UART assertion requires an observation upper bound",
                    ));
                }
                observed.push(Observation {
                    endpoint: endpoint.unwrap(),
                    timestamp_ns: event.mapped_time_ns,
                    data: ObservationData::Uart(data),
                });
            }
            Observed::Digital(Some(level))
                if endpoint.is_some_and(|e| {
                    self.observations
                        .get(e)
                        .is_some_and(|kind| kind == "digital")
                }) =>
            {
                if event.mapped_time_ns != event.delivery_time_ns {
                    return Err(admission("digital sample requires its completed cut"));
                }
                digital.push((endpoint.unwrap(), level));
            }
            #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
            Observed::Snapshot(snapshot) => {
                if let Some(endpoints) = self.snapshots.get(&event.source_node) {
                    if event.mapped_time_ns != event.delivery_time_ns {
                        return Err(admission("digital snapshot requires its completed cut"));
                    }
                    for (qualified, local) in endpoints {
                        digital.push((
                            qualified.as_str(),
                            crate::boards::digital_level(local, snapshot)?,
                        ));
                    }
                }
            }
            _ => {}
        }
        if observed.is_empty() && digital.is_empty() {
            return Ok(());
        }
        let mut matches = vec![];
        for group in &mut self.groups {
            if group.ready
                && !self.ready.is_empty()
                && event.delivery_time_ns <= group.evaluator.origin_ns()
            {
                continue;
            }
            let before = group.evaluator.status().to_vec();
            group
                .evaluator
                .observe(&digital, &observed, event.delivery_time_ns, false)
                .map_err(lower)?;
            for ((index, old), new) in group
                .indices
                .iter()
                .zip(&before)
                .zip(group.evaluator.status())
            {
                if *old == Status::Pending && *new == Status::Pass {
                    matches.push(Self::receipt(event, Some(*index)));
                }
            }
        }
        matches.sort_by_key(|receipt| receipt.assertion_index);
        self.matches.extend(matches);
        Ok(())
    }
    fn observe_ready<T>(
        &mut self,
        event: &Event<HostInput, T>,
        endpoint: Option<&str>,
        data: &[u8],
    ) -> Result<()> {
        if self.origin_ns.is_some() {
            return Ok(());
        }
        for (index, item) in self.ready.iter().enumerate() {
            let name = item.endpoint.to_string();
            if self.ready_receipts.contains_key(&item.name)
                || endpoint != Some(name.as_str())
                || self
                    .observations
                    .get(&name)
                    .is_none_or(|kind| kind != &event.kind)
            {
                continue;
            }
            if event.quality == TimeQuality::LowerBound
                || event.mapped_time_ns > event.delivery_time_ns
            {
                return Err(admission(
                    "UART ready requires bytes with an observation upper bound",
                ));
            }
            let mut combined = std::mem::take(&mut self.tails[index]);
            combined.extend(data);
            if combined
                .windows(item.text.len())
                .any(|part| part == item.text)
            {
                self.ready_receipts
                    .insert(item.name.clone(), Self::receipt(event, None));
            } else {
                let keep = (item.text.len() - 1).min(combined.len());
                self.tails[index] = combined[combined.len() - keep..].to_vec();
            }
        }
        if self.ready_receipts.len() == self.ready.len() {
            event
                .delivery_time_ns
                .checked_add(self.duration_ns)
                .ok_or_else(|| admission("experiment ready origin overflows time"))?;
            self.origin_ns = Some(event.delivery_time_ns);
            self.start_group(true, event.delivery_time_ns)?;
        }
        Ok(())
    }
    #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
    fn board<T>(
        &mut self,
        event: &Event<HostInput, T>,
        snapshot: &serde_json::Value,
    ) -> Result<()> {
        if !self.machine_nodes.contains(&event.source_node) {
            return Ok(());
        }
        let source = event
            .source_time_ns
            .ok_or_else(|| admission("machine snapshot lacks native timestamp"))?;
        if snapshot["timestamp_ns"].as_u64() != Some(source)
            || event.mapped_time_ns != event.delivery_time_ns
            || source < event.mapped_time_ns
        {
            return Err(admission(
                "machine snapshot must preserve its native epoch and completed cut",
            ));
        }
        let epoch = source - event.mapped_time_ns;
        let old = self.boards.get(&event.source_node);
        if old.is_some_and(|value| {
            value["native_origin_ns"].as_u64() != Some(epoch)
                || value["last_source_ns"]
                    .as_u64()
                    .is_none_or(|previous| source < previous)
        }) {
            return Err(admission(
                "machine snapshot native epoch changed or moved backwards",
            ));
        }
        let count = old.map_or(Ok(1), |v| {
            v["observation_batches"]
                .as_u64()
                .unwrap()
                .checked_add(1)
                .ok_or_else(|| admission("board observation count overflow"))
        })?;
        self.boards.insert(event.source_node.clone(),serde_json::json!({"native_origin_ns":epoch,
            "first_source_ns":old.map_or(source,|v|v["first_source_ns"].as_u64().unwrap()),"last_source_ns":source,
            "first_mapped_ns":old.map_or(event.mapped_time_ns,|v|v["first_mapped_ns"].as_u64().unwrap()),"last_mapped_ns":event.mapped_time_ns,"observation_batches":count}));
        Ok(())
    }
}

pub(crate) type CutCallback = Rc<dyn Fn(u64) -> Result<()>>;
struct CutObserver(Rc<RefCell<Consumer>>, Option<CutCallback>);
impl Sampler<HostInput, HostEvent> for CutObserver {
    fn sample(&mut self, cut: u64, _: &HostContext) -> Result<()> {
        self.0.borrow_mut().begin_cut(cut)?;
        if let Some(callback) = &self.1 {
            callback(cut)?;
        }
        Ok(())
    }
}
impl Component<HostInput, HostEvent> for CutObserver {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
    fn sampler(&mut self) -> Option<&mut dyn Sampler<HostInput, HostEvent>> {
        Some(self)
    }
}
struct CutObserverFactory(Rc<RefCell<Consumer>>, Option<CutCallback>);
impl Factory<HostOptions, HostInput, HostEvent> for CutObserverFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        if !matches!(node.options, HostOptions::ObservationCut) {
            return Err(admission(
                "experiment cut observer requires its internal options",
            ));
        }
        Ok(NodeDescription::new(vec![], [Capability::Sample]))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        Ok(Box::new(CutObserver(self.0.clone(), self.1.clone())))
    }
}

pub(crate) fn admit_consumer(scene: &Scene, options: SimulationOptions) -> Result<Consumer> {
    if options.initial_session
        != (sim_core::Session {
            session_id: 1,
            t_sim_ns: 0,
        })
        || scene.mode != sim_core::RunMode::Stepped
        || scene.topology.nodes.len() > 63
    {
        return Err(admission(
            "finite experiment requires stepped mode, at most 63 nodes and session1 at time0",
        ));
    }
    let empty = Default::default();
    let experiment = scene.experiment.as_ref().unwrap_or(&empty);
    let simulation = scene.simulation(options)?;
    let descriptions = scene
        .topology
        .nodes
        .iter()
        .zip(simulation.validate())
        .map(|(n, d)| (n.name.clone(), d.clone()))
        .collect();
    let connected = scene
        .topology
        .links
        .iter()
        .flat_map(|l| [l.source.to_string(), l.target.to_string()])
        .collect();
    let consumer = Consumer::new(
        experiment.assertions.clone(),
        scene.duration_ns,
        experiment.ready.clone(),
        &descriptions,
        &connected,
    )?;
    if experiment.inputs.len() > 1024 {
        return Err(admission("too many experiment inputs"));
    }
    for input in &experiment.inputs {
        if input.at_ns > scene.duration_ns
            || matches!(&input.origin,Origin::Marker(name) if !experiment.ready.iter().any(|v|&v.name==name))
        {
            return Err(admission("invalid experiment input origin or horizon"));
        }
        simulation.validate_input(&input.endpoint, input.payload.clone())?;
    }
    Ok(consumer)
}
fn enqueue(
    control: &sim_core::Control<HostInput, HostEvent>,
    consumer: &Rc<RefCell<Consumer>>,
    inputs: &[ExperimentInput],
    scheduled: &RefCell<BTreeSet<usize>>,
) -> Result<()> {
    let known = {
        let state = consumer.borrow();
        let mut known = vec![];
        for session in [true, false] {
            for (index, input) in inputs
                .iter()
                .enumerate()
                .filter(|(_, input)| matches!(input.origin, Origin::Session) == session)
            {
                if scheduled.borrow().contains(&index) {
                    continue;
                }
                let origin = match &input.origin {
                    Origin::Session => Some(0),
                    Origin::Ready => state.origin_ns,
                    Origin::Marker(name) => {
                        state.ready_receipts.get(name).map(|r| r.delivery_time_ns)
                    }
                };
                if let Some(origin) = origin {
                    known.push((
                        index,
                        input,
                        origin
                            .checked_add(input.at_ns)
                            .ok_or_else(|| admission("experiment input time overflows"))?,
                    ));
                }
            }
        }
        known
    };
    for (index, input, time) in known {
        control.schedule_input(input.endpoint.clone(), input.payload.clone(), Some(time))?;
        scheduled.borrow_mut().insert(index);
    }
    Ok(())
}

/// Return a result only after component cleanup. A failure keeps the original
/// allocation and separately reports cleanup notes and partial committed work.
pub fn run_experiment(
    scene: &Scene,
    options: SimulationOptions,
    on_event: Option<Observer<HostInput, HostEvent>>,
) -> std::result::Result<ExperimentResult, RunError> {
    run_with_hooks(scene, options, on_event, None, None)
}

pub(crate) fn run_with_hooks(
    scene: &Scene,
    mut options: SimulationOptions,
    on_event: Option<Observer<HostInput, HostEvent>>,
    on_cut: Option<CutCallback>,
    before_event: Option<Observer<HostInput, HostEvent>>,
) -> std::result::Result<ExperimentResult, RunError> {
    let mut simulation = None;
    let mut consumer = None;
    let execution = (|| -> Result<()> {
        if options.initial_session
            != (sim_core::Session {
                session_id: 1,
                t_sim_ns: 0,
            })
        {
            return Err(admission(
                "finite experiment requires one session starting at time0",
            ));
        }
        let empty = Default::default();
        let experiment = scene.experiment.as_ref().unwrap_or(&empty);
        let state = Rc::new(RefCell::new(admit_consumer(scene, options.clone())?));
        let begun = Instant::now();
        let total_deadline = begun
            .checked_add(options.host_timeout.unwrap_or(Duration::from_secs(30)))
            .ok_or_else(|| admission("host timeout overflows Instant"))?;
        let startup_deadline = total_deadline.min(
            begun
                .checked_add(options.startup_timeout)
                .ok_or_else(|| admission("startup timeout overflows Instant"))?,
        );
        let mut registry = runtime::registry()?;
        let mut name = "_experiment_cut".to_owned();
        while scene.topology.nodes.iter().any(|node| node.name == name) {
            name.push('_');
        }
        registry.register(&name, CutObserverFactory(state.clone(), on_cut))?;
        let mut nodes = scene.topology.nodes.clone();
        nodes.push(NodeSpec::new(&name, &name, HostOptions::ObservationCut));
        options.quantum_ns = scene.quantum_ns;
        options.mode = scene.mode;
        options.max_pending_inputs = options.max_pending_inputs.max(experiment.inputs.len());
        let mut owner = Simulation::new(
            Topology::new(nodes, scene.topology.links.clone())?,
            registry,
            options,
        )?;
        let observed = state.clone();
        let started = Rc::new(Cell::new(false));
        let startup_running = Rc::new(Cell::new(false));
        let scheduled = Rc::new(RefCell::new(BTreeSet::new()));
        let inputs = Rc::new(experiment.inputs.clone());
        let event_started = started.clone();
        let event_startup = startup_running.clone();
        let event_scheduled = scheduled.clone();
        let event_inputs = inputs.clone();
        owner.set_observer(Some(Rc::new(move |event, control| {
            if let Some(callback) = &before_event {
                callback(event, control)?;
            }
            observed.borrow_mut().event(event)?;
            if event_started.get() {
                enqueue(control, &observed, &event_inputs, &event_scheduled)?;
                if event_startup.get() && observed.borrow().origin_ns.is_some() {
                    control.pause()?;
                }
            }
            if let Some(callback) = &on_event {
                callback(event, control)?;
            }
            Ok(())
        })));
        consumer = Some(state);
        simulation = Some(owner);
        let simulation = simulation.as_mut().unwrap();
        simulation.start(Some(startup_deadline))?;
        started.set(true);
        enqueue(
            &simulation.control(),
            consumer.as_ref().unwrap(),
            &inputs,
            &scheduled,
        )?;
        while consumer.as_ref().unwrap().borrow().origin_ns.is_none() {
            let target = simulation
                .t_sim_ns()
                .checked_add(scene.quantum_ns)
                .ok_or_else(|| admission("startup target overflows time"))?;
            startup_running.set(true);
            simulation.run_until(target, Some(startup_deadline))?;
            startup_running.set(false);
        }
        let end = consumer
            .as_ref()
            .unwrap()
            .borrow()
            .origin_ns
            .unwrap()
            .checked_add(scene.duration_ns)
            .ok_or_else(|| admission("experiment end overflows time"))?;
        let result = simulation.run_until(end, Some(total_deadline))?;
        if result.status != StepStatus::Ok || result.reached_time_ns != end {
            return Err(admission(
                "experiment did not complete its requested duration",
            ));
        }
        consumer.as_ref().unwrap().borrow_mut().finish(end)?;
        Ok(())
    })();
    let mut primary = execution.err();
    let mut notes = Vec::new();
    let mut time_ns = 0;
    let mut events_total = 0;
    if let Some(simulation) = &mut simulation {
        time_ns = simulation.t_sim_ns();
        events_total = simulation.events_total();
        if let Err(error) = simulation.close() {
            if let Some(first) = &primary {
                if !Arc::ptr_eq(first, &error) {
                    notes.push(format!("Simulation cleanup: {error}"));
                }
            } else {
                primary = Some(error);
            }
        }
        notes.extend(simulation.failure_notes());
    }
    if let Some(primary) = primary {
        return Err(RunError {
            primary,
            notes,
            time_ns,
            events_total,
        });
    }
    let consumer = consumer.unwrap();
    let consumer = consumer.borrow();
    Ok(consumer.result(time_ns, events_total))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::assertions::{Assertion, Condition};

    fn event(sequence: u64, node: &str, mapped: u64, delivery: u64) -> Event<HostInput, ()> {
        Event {
            session_id: 1,
            sequence,
            source_node: node.into(),
            source_port: Some("uart1".into()),
            kind: "uart".into(),
            payload: EventPayload::Observation(()),
            source_time_ns: Some(100 + mapped),
            mapped_time_ns: mapped,
            delivery_time_ns: delivery,
            quality: TimeQuality::UpperBound,
        }
    }
    #[test]
    fn split_markers_keep_receipts_and_session_ready_deadlines_separate() {
        let descriptions = ["a", "b"]
            .map(|name| {
                (
                    name.to_owned(),
                    NodeDescription::new(vec![sim_core::PortSpec::new("uart1", "uart")], []),
                )
            })
            .into();
        let assertion = |endpoint: &str, text: &[u8], before_ns, origin| ExperimentAssertion {
            assertion: Assertion {
                before_ns,
                endpoint: endpoint.into(),
                condition: Condition::UartContains(text.to_vec()),
            },
            origin,
        };
        let mut consumer = Consumer::new(
            vec![
                assertion("a.uart1", b"OK", 5, Origin::Session),
                assertion("a.uart1", b"OK", 2, Origin::Ready),
                assertion("b.uart1", b"RDY", 6, Origin::Session),
            ],
            2,
            vec![],
            &descriptions,
            &BTreeSet::new(),
        );
        assert!(
            consumer.is_err(),
            "assertion deadlines must fit the active duration"
        );
        consumer = Consumer::new(
            vec![
                assertion("a.uart1", b"OK", 5, Origin::Session),
                assertion("a.uart1", b"OK", 2, Origin::Ready),
                assertion("b.uart1", b"RDY", 6, Origin::Session),
            ],
            6,
            vec![
                ReadyMarker {
                    name: "first".into(),
                    endpoint: sim_core::Endpoint::parse("a.uart1").unwrap(),
                    text: b"OK".to_vec(),
                },
                ReadyMarker {
                    name: "second".into(),
                    endpoint: sim_core::Endpoint::parse("b.uart1").unwrap(),
                    text: b"RDY".to_vec(),
                },
            ],
            &descriptions,
            &BTreeSet::new(),
        );
        let mut consumer = consumer.unwrap();
        consumer
            .observe(&event(0, "a", 1, 1), Observed::Uart(b"O", None))
            .unwrap();
        consumer
            .observe(&event(1, "a", 2, 3), Observed::Uart(b"K", None))
            .unwrap();
        assert_eq!(consumer.origin_ns, None);
        consumer
            .observe(&event(2, "b", 4, 5), Observed::Uart(b"RDY", None))
            .unwrap();
        consumer
            .observe(&event(3, "a", 5, 5), Observed::Uart(b"OK", None))
            .unwrap();
        assert_eq!(
            consumer.result(5, 4).status,
            [Status::Pass, Status::Pending, Status::Pass]
        );
        consumer
            .observe(&event(4, "a", 6, 6), Observed::Uart(b"OK", None))
            .unwrap();
        consumer.finish(11).unwrap();
        let result = consumer.result(11, 5);
        assert!(result.passed());
        assert_eq!(result.origin_ns, 5);
        assert_eq!(
            (
                result.ready_receipts["first"].assertion_index,
                result.ready_receipts["first"].event_sequence,
                result.ready_receipts["first"].source_time_ns,
                result.ready_receipts["first"].mapped_time_ns,
                result.ready_receipts["first"].delivery_time_ns
            ),
            (None, 1, Some(102), 2, 3)
        );
        assert_eq!(
            result
                .matches
                .iter()
                .map(|r| (r.assertion_index, r.event_sequence))
                .collect::<Vec<_>>(),
            [(Some(0), 1), (Some(2), 2), (Some(1), 4)]
        );
    }

    #[cfg(any(feature = "trace", all(target_os = "linux", feature = "machine")))]
    #[test]
    fn board_receipt_requires_one_native_epoch_and_unchanged_completed_cut() {
        let descriptions = BTreeMap::from([(
            "a".into(),
            NodeDescription::new(vec![], [Capability::Machine]),
        )]);
        let mut consumer =
            Consumer::new(vec![], 4, vec![], &descriptions, &BTreeSet::new()).unwrap();
        let mut first = event(0, "a", 0, 0);
        first.kind = "snapshot".into();
        first.source_port = Some("snapshot".into());
        consumer
            .observe(
                &first,
                Observed::Snapshot(&serde_json::json!({"timestamp_ns":100})),
            )
            .unwrap();
        let mut second = event(1, "a", 2, 2);
        second.kind = "snapshot".into();
        consumer
            .observe(
                &second,
                Observed::Snapshot(&serde_json::json!({"timestamp_ns":102})),
            )
            .unwrap();
        assert_eq!(
            consumer.result(2, 2).boards["a"],
            serde_json::json!({"native_origin_ns":100,"first_source_ns":100,
            "last_source_ns":102,"first_mapped_ns":0,"last_mapped_ns":2,"observation_batches":2})
        );
        let mut changed = event(2, "a", 3, 3);
        changed.source_time_ns = Some(104);
        assert!(consumer
            .observe(
                &changed,
                Observed::Snapshot(&serde_json::json!({"timestamp_ns":104}))
            )
            .is_err());
        assert_eq!(consumer.result(2, 2).boards["a"]["last_source_ns"], 102);
    }
}
