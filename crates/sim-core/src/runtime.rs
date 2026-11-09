//! Synchronous scheduling and explicit component ownership.

use crate::{
    data::{admission, state_error},
    topology::{construction_rank, owner_capability},
    *,
};
use std::{
    any::Any,
    cell::RefCell,
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet, BinaryHeap, VecDeque},
    marker::PhantomData,
    rc::{Rc, Weak},
    sync::{
        atomic::{AtomicBool, Ordering as AtomicOrdering},
        Arc,
    },
    time::{Duration, Instant},
};

pub trait Machine<I: Clone + 'static, E: Clone + 'static> {
    fn prepare_step(&mut self, step: &StepContext, context: &Context<I, E>) -> Result<()>;
    fn request_step(&mut self, step: &StepContext, context: &Context<I, E>) -> Result<()>;
    fn finish_step(&mut self, step: &StepContext, context: &Context<I, E>)
        -> Result<StepResult<E>>;
}

pub trait Stepper<I: Clone + 'static, E: Clone + 'static> {
    fn step(&mut self, step: &StepContext, context: &Context<I, E>) -> Result<StepResult<E>>;
}

pub trait Input<I: Clone + 'static, E: Clone + 'static> {
    fn input(&mut self, port: &str, value: I, context: &Context<I, E>) -> Result<()>;
}

pub trait Sampler<I: Clone + 'static, E: Clone + 'static> {
    fn sample(&mut self, cut: u64, context: &Context<I, E>) -> Result<()>;
}

pub trait InputCommit<I: Clone + 'static, E: Clone + 'static> {
    fn commit_inputs(&mut self, cut: u64, context: &Context<I, E>) -> Result<()>;
}

pub trait Dispatcher<I: Clone + 'static, E: Clone + 'static> {
    fn dispatch_budget(&self) -> usize;
    fn next_due_ns(&self) -> Result<Option<u64>>;
    fn dispatch(&mut self, cut: u64, budget: usize, context: &Context<I, E>) -> Result<usize>;
}

/// Only factory wiring erases attachment types. The borrower uses attach<T,R>
/// and adapters check the concrete endpoint type before changing wiring.
pub struct Attachment<I: Clone + 'static, E: Clone + 'static> {
    pub descriptor: PortSpec,
    pub endpoint_name: String,
    pub owner_port: String,
    pub endpoint: Box<dyn Any>,
    pub context: Context<I, E>,
}

pub trait ConnectionOwner<I: Clone + 'static, E: Clone + 'static> {
    fn attach_port(&mut self, attachment: Attachment<I, E>) -> Result<Box<dyn Any>>;
}

pub type HealthCheck = Rc<dyn Fn() -> Result<()>>;

/// Optional interfaces are admitted against the pure description immediately
/// after registering the returned owner. Lifecycle cleanup always remains with
/// Simulation; factories must clean up construction that never returned.
pub trait Component<I: Clone + 'static, E: Clone + 'static>: Any {
    fn close(&mut self) -> Result<()>;
    fn reset(&mut self, _session: Session, _context: &Context<I, E>) -> Result<()> {
        Ok(())
    }
    fn start(&mut self, _context: &Context<I, E>) -> Result<()> {
        Ok(())
    }
    fn machine(&mut self) -> Option<&mut dyn Machine<I, E>> {
        None
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<I, E>> {
        None
    }
    fn input(&mut self) -> Option<&mut dyn Input<I, E>> {
        None
    }
    fn sampler(&mut self) -> Option<&mut dyn Sampler<I, E>> {
        None
    }
    fn input_commit(&mut self) -> Option<&mut dyn InputCommit<I, E>> {
        None
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<I, E>> {
        None
    }
    fn connection_owner(&mut self) -> Option<&mut dyn ConnectionOwner<I, E>> {
        None
    }
    /// A health closure borrows adapter state; it does not own or close it.
    fn health_check(&self) -> Option<HealthCheck> {
        None
    }
}

pub trait InputValidator<O, I> {
    fn validate_input(
        &self,
        node: &NodeSpec<O>,
        port: &str,
        value: I,
        connected_ports: &[String],
    ) -> Result<I>;
}

pub trait Factory<O: 'static, I: Clone + 'static, E: Clone + 'static>: 'static {
    fn describe(&self, node: &NodeSpec<O>) -> Result<NodeDescription>;
    fn create(
        &self,
        node: &NodeSpec<O>,
        context: &mut BuildContext<'_, O, I, E>,
    ) -> Result<Box<dyn Component<I, E>>>;
    fn input_validator(&self) -> Option<&dyn InputValidator<O, I>> {
        None
    }
    fn validate_topology(
        &self,
        _topology: &Topology<O>,
        _descriptions: &[NodeDescription],
        _registry: &Registry<O, I, E>,
    ) -> Result<()> {
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FactoryId(usize);

/// Explicit compiled factories and aliases; no dynamic module loading or ABI.
pub struct Registry<O: 'static, I: Clone + 'static, E: Clone + 'static> {
    kinds: BTreeMap<String, FactoryId>,
    factories: Vec<Rc<dyn Factory<O, I, E>>>,
}

impl<O: 'static, I: Clone + 'static, E: Clone + 'static> Default for Registry<O, I, E> {
    fn default() -> Self {
        Self {
            kinds: BTreeMap::new(),
            factories: Vec::new(),
        }
    }
}

impl<O: 'static, I: Clone + 'static, E: Clone + 'static> Registry<O, I, E> {
    pub fn register(
        &mut self,
        kind: impl Into<String>,
        factory: impl Factory<O, I, E>,
    ) -> Result<FactoryId> {
        let kind = kind.into();
        self.check_kind(&kind)?;
        let id = FactoryId(self.factories.len());
        self.factories.push(Rc::new(factory));
        self.kinds.insert(kind, id);
        Ok(id)
    }

    pub fn alias(&mut self, kind: impl Into<String>, factory: FactoryId) -> Result<()> {
        let kind = kind.into();
        self.check_kind(&kind)?;
        if factory.0 >= self.factories.len() {
            return Err(admission("unknown factory ID"));
        }
        self.kinds.insert(kind, factory);
        Ok(())
    }

    fn check_kind(&self, kind: &str) -> Result<()> {
        if kind.is_empty()
            || kind.len() > 128
            || kind.contains('\0')
            || self.kinds.contains_key(kind)
        {
            return Err(admission("invalid or duplicate factory kind"));
        }
        Ok(())
    }

    fn get(&self, kind: &str) -> Result<(FactoryId, &dyn Factory<O, I, E>)> {
        let id = *self
            .kinds
            .get(kind)
            .ok_or_else(|| admission(format!("unknown component kind {kind:?}")))?;
        Ok((id, self.factories[id.0].as_ref()))
    }
}

struct Plan<O: 'static, I: Clone + 'static, E: Clone + 'static> {
    topology: Topology<O>,
    registry: Registry<O, I, E>,
    descriptions: Vec<NodeDescription>,
    names: BTreeMap<String, usize>,
    peers: BTreeMap<Endpoint, Vec<Endpoint>>,
    construction: Vec<usize>,
}

impl<O: 'static, I: Clone + 'static, E: Clone + 'static> Plan<O, I, E> {
    fn admit(topology: Topology<O>, registry: Registry<O, I, E>) -> Result<Self> {
        let topology = Topology::new(topology.nodes, topology.links)?;
        let mut descriptions = Vec::new();
        let names: BTreeMap<_, _> = topology
            .nodes
            .iter()
            .enumerate()
            .map(|(index, node)| (node.name.clone(), index))
            .collect();
        for node in &topology.nodes {
            let (_, factory) = registry.get(&node.kind)?;
            let description = factory.describe(node)?;
            description.validate()?;
            if !node
                .required_capabilities
                .is_subset(&description.capabilities)
            {
                return Err(admission(format!(
                    "node {} lacks required capabilities",
                    node.name
                )));
            }
            for port in &description.ports {
                if port.capabilities.contains("external_input")
                    && (port.direction == Direction::Out
                        || !description.has(Capability::Input)
                        || factory.input_validator().is_none())
                {
                    return Err(admission(
                        "external_input requires an input node and pure factory validator",
                    ));
                }
            }
            if description.has(Capability::Machine) && description.has(Capability::Step)
                || description.has(Capability::Connection)
                    && (description.has(Capability::Machine) || description.has(Capability::Step))
            {
                return Err(admission("a node must have one execution owner"));
            }
            descriptions.push(description);
        }
        let mut seen = BTreeSet::new();
        let mut counts = BTreeMap::<Endpoint, usize>::new();
        let mut borrowers = BTreeSet::new();
        let mut peers = BTreeMap::<Endpoint, Vec<Endpoint>>::new();
        for link in &topology.links {
            let first = *names
                .get(&link.source.node)
                .ok_or_else(|| admission("link refers to unknown node"))?;
            let second = *names
                .get(&link.target.node)
                .ok_or_else(|| admission("link refers to unknown node"))?;
            let a = descriptions[first].port(&link.source.port)?;
            let b = descriptions[second].port(&link.target.port)?;
            let identity = if link.source <= link.target {
                (&link.source, &link.target)
            } else {
                (&link.target, &link.source)
            };
            if link.source == link.target || !seen.insert(identity) {
                return Err(admission("self connection or duplicate connection"));
            }
            if a.category != b.category
                || a.protocol != b.protocol
                || a.direction == b.direction && a.direction != Direction::InOut
                || !a.required_capabilities.is_subset(&b.capabilities)
                || !b.required_capabilities.is_subset(&a.capabilities)
                || !link.required_capabilities.is_subset(&a.capabilities)
                || !link.required_capabilities.is_subset(&b.capabilities)
            {
                return Err(admission(
                    "connection category/protocol/direction/capability mismatch",
                ));
            }
            for (endpoint, port) in [(&link.source, a), (&link.target, b)] {
                let count = counts.entry(endpoint.clone()).or_default();
                *count += 1;
                if port.max_connections.is_some_and(|maximum| *count > maximum) {
                    return Err(admission(format!(
                        "port connection capacity exceeded: {endpoint}"
                    )));
                }
            }
            let owner = owner_capability(a.category);
            let first_owner = descriptions[first].has(owner.clone());
            if first_owner == descriptions[second].has(owner) {
                return Err(admission(
                    "each link requires exactly one connection/binding owner",
                ));
            }
            let (owner, borrower, endpoint) = if first_owner {
                (first, second, &link.target)
            } else {
                (second, first, &link.source)
            };
            if !borrowers.insert(endpoint.clone())
                || construction_rank(&descriptions[owner])
                    >= construction_rank(&descriptions[borrower])
            {
                return Err(admission(
                    "borrower requires exactly one owner in an earlier construction group",
                ));
            }
            peers
                .entry(link.source.clone())
                .or_default()
                .push(link.target.clone());
            peers
                .entry(link.target.clone())
                .or_default()
                .push(link.source.clone());
        }
        for (node, description) in topology.nodes.iter().zip(&descriptions) {
            for port in &description.ports {
                if counts
                    .get(&Endpoint::new(&node.name, &port.name)?)
                    .copied()
                    .unwrap_or(0)
                    < port.min_connections
                {
                    return Err(admission(format!(
                        "port {}.{} requires more connections",
                        node.name, port.name
                    )));
                }
            }
        }
        let mut checked = BTreeSet::new();
        for node in &topology.nodes {
            let (id, factory) = registry.get(&node.kind)?;
            if checked.insert(id.0) {
                factory.validate_topology(&topology, &descriptions, &registry)?;
            }
        }
        let mut construction: Vec<_> = (0..topology.nodes.len()).collect();
        construction.sort_by_key(|&index| construction_rank(&descriptions[index]));
        Ok(Self {
            topology,
            registry,
            descriptions,
            names,
            peers,
            construction,
        })
    }

    fn validate_input(&self, endpoint: &Endpoint, input: I) -> Result<I> {
        let index = *self
            .names
            .get(&endpoint.node)
            .ok_or_else(|| admission("unknown input node"))?;
        let node = &self.topology.nodes[index];
        let description = &self.descriptions[index];
        let port = description.port(&endpoint.port)?;
        if !description.has(Capability::Input)
            || port.direction == Direction::Out
            || !port.capabilities.contains("external_input")
        {
            return Err(admission("endpoint cannot accept external input"));
        }
        let connected: Vec<_> = description
            .ports
            .iter()
            .filter(|port| {
                self.peers.contains_key(&Endpoint {
                    node: node.name.clone(),
                    port: port.name.clone(),
                })
            })
            .map(|port| port.name.clone())
            .collect();
        let (_, factory) = self.registry.get(&node.kind)?;
        factory
            .input_validator()
            .expect("admitted input validator")
            .validate_input(node, &endpoint.port, input, &connected)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SimulationState {
    New,
    Paused,
    Running,
    Failed,
    Closed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RunMode {
    Stepped,
    Interactive,
}

#[derive(Clone, Debug)]
pub struct SimulationOptions {
    pub quantum_ns: u64,
    pub mode: RunMode,
    pub startup_timeout: Duration,
    pub host_timeout: Option<Duration>,
    pub max_pending_inputs: usize,
    pub dispatch_budget: usize,
    pub event_history: usize,
    pub initial_session: Session,
    /// Borrowed cancellation request, checked at the same boundaries as health
    /// and deadlines. Cancellation preserves partial effects and needs cleanup.
    pub cancellation: Option<Arc<AtomicBool>>,
}

impl Default for SimulationOptions {
    fn default() -> Self {
        Self {
            quantum_ns: 1_000_000,
            mode: RunMode::Stepped,
            startup_timeout: Duration::from_secs(10),
            host_timeout: None,
            max_pending_inputs: 256,
            dispatch_budget: 4096,
            event_history: 256,
            initial_session: Session {
                session_id: 1,
                t_sim_ns: 0,
            },
            cancellation: None,
        }
    }
}

struct Pending<I> {
    when: u64,
    sequence: u64,
    endpoint: Endpoint,
    input: I,
}
impl<I> PartialEq for Pending<I> {
    fn eq(&self, other: &Self) -> bool {
        (self.when, self.sequence) == (other.when, other.sequence)
    }
}
impl<I> Eq for Pending<I> {}
impl<I> PartialOrd for Pending<I> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl<I> Ord for Pending<I> {
    fn cmp(&self, other: &Self) -> Ordering {
        (other.when, other.sequence).cmp(&(self.when, self.sequence))
    }
}

pub type Observer<I, E> = Rc<dyn Fn(&Event<I, E>, &Control<I, E>) -> Result<()>>;
type InputAdmission<I> = Rc<dyn Fn(&Endpoint, I) -> Result<I>>;

struct Shared<I: Clone + 'static, E: Clone + 'static> {
    identity: Rc<()>,
    generation: u64,
    state: SimulationState,
    session: Option<Session>,
    completed_time: u64,
    dispatch_time: u64,
    step_id: u64,
    phase: &'static str,
    failure: Option<Failure>,
    notes: Vec<String>,
    closing: bool,
    stepping: bool,
    checking_health: bool,
    pause_requested: bool,
    committing_cut: Option<u64>,
    deadline: Option<Instant>,
    host_deadline: Option<Instant>,
    borrowed_deadline: bool,
    health: Vec<HealthCheck>,
    cancellation: Option<Arc<AtomicBool>>,
    inputs: BinaryHeap<Pending<I>>,
    input_sequence: u64,
    max_pending_inputs: usize,
    validate_input: InputAdmission<I>,
    events: VecDeque<Event<I, E>>,
    event_history: usize,
    events_total: u64,
    events_dropped: u64,
    observer: Option<Observer<I, E>>,
}

/// A borrowed session capability, without resource or lifecycle ownership.
/// Observer callbacks may schedule, pause and report failure. Reset/close need
/// the unique mutable Simulation and cannot reenter a component callback.
#[derive(Clone)]
pub struct Control<I: Clone + 'static, E: Clone + 'static> {
    shared: Weak<RefCell<Shared<I, E>>>,
    generation: u64,
}

impl<I: Clone + 'static, E: Clone + 'static> Control<I, E> {
    fn active(&self) -> Result<Rc<RefCell<Shared<I, E>>>> {
        let shared = self
            .shared
            .upgrade()
            .ok_or_else(|| state_error("simulation is no longer owned"))?;
        if shared.borrow().generation != self.generation
            || shared.borrow().state == SimulationState::Closed
        {
            return Err(state_error("component belongs to a closed session"));
        }
        Ok(shared)
    }

    pub fn fail(&self, error: Failure) -> Failure {
        let Some(shared) = self.shared.upgrade() else {
            return error;
        };
        let mut shared = shared.borrow_mut();
        if shared.generation != self.generation
            || shared.closing
            || shared.state == SimulationState::Closed
        {
            return error;
        }
        if let Some(primary) = shared.failure.clone() {
            if !Arc::ptr_eq(&primary, &error) {
                shared
                    .notes
                    .push(format!("later simulation failure: {error}"));
            }
            shared.state = SimulationState::Failed;
            primary
        } else {
            shared.failure = Some(error.clone());
            shared.state = SimulationState::Failed;
            error
        }
    }

    pub fn check_io(&self) -> Result<()> {
        let shared = self.active()?;
        let shared = shared.borrow();
        if let Some(failure) = &shared.failure {
            return Err(failure.clone());
        }
        if shared.phase == "creating" || shared.closing {
            return Err(state_error(
                "connection IO requires completed healthy wiring",
            ));
        }
        Ok(())
    }

    pub fn check_deadline(&self) -> Result<()> {
        let shared = self.active()?;
        let checks = {
            let mut state = shared.borrow_mut();
            if let Some(failure) = &state.failure {
                return Err(failure.clone());
            }
            if state.closing {
                return Err(state_error("deadline checks cannot run during cleanup"));
            }
            if state
                .cancellation
                .as_ref()
                .is_some_and(|flag| flag.load(AtomicOrdering::Relaxed))
            {
                drop(state);
                return Err(self.fail(Arc::new(Error::Interrupted(
                    "simulation interrupted".into(),
                ))));
            }
            if state
                .deadline
                .is_some_and(|deadline| Instant::now() >= deadline)
            {
                let failure: Failure = Arc::new(Error::Deadline(format!(
                    "host deadline exceeded during {}",
                    state.phase
                )));
                drop(state);
                return Err(self.fail(failure));
            }
            if state.checking_health {
                drop(state);
                return Err(self.fail(state_error("health checks cannot reenter the runtime")));
            }
            state.checking_health = true;
            state.health.clone()
        };
        let mut result = Ok(());
        for check in checks {
            if let Err(error) = check() {
                result = Err(self.fail(error));
                break;
            }
            if let Some(error) = shared.borrow().failure.clone() {
                result = Err(error);
                break;
            }
        }
        shared.borrow_mut().checking_health = false;
        if result.is_ok() {
            let state = shared.borrow();
            if state
                .deadline
                .is_some_and(|deadline| Instant::now() >= deadline)
            {
                let error: Failure = Arc::new(Error::Deadline(format!(
                    "host deadline exceeded during {}",
                    state.phase
                )));
                drop(state);
                return Err(self.fail(error));
            }
        }
        result
    }

    pub fn t_sim_ns(&self) -> Result<u64> {
        Ok(self.active()?.borrow().dispatch_time)
    }
    pub fn session(&self) -> Result<Session> {
        self.active()?
            .borrow()
            .session
            .ok_or_else(|| state_error("session is not started"))
    }
    pub fn deadline(&self) -> Result<Option<Instant>> {
        Ok(self.active()?.borrow().deadline)
    }

    pub fn pause(&self) -> Result<()> {
        let shared = self.active()?;
        let mut shared = shared.borrow_mut();
        if !matches!(
            shared.state,
            SimulationState::Paused | SimulationState::Running
        ) {
            return Err(state_error("pause requires a started healthy Simulation"));
        }
        shared.pause_requested = true;
        Ok(())
    }

    pub fn schedule_input(&self, endpoint: Endpoint, input: I, at_ns: Option<u64>) -> Result<()> {
        let shared = self.active()?;
        let (when, validator) = {
            let state = shared.borrow();
            if let Some(error) = &state.failure {
                return Err(error.clone());
            }
            let when = at_ns.unwrap_or(state.dispatch_time);
            if state.committing_cut.is_some_and(|cut| when <= cut) {
                drop(state);
                return Err(self.fail(state_error(
                    "inputs cannot target the active input commit cut",
                )));
            }
            if !(matches!(
                state.state,
                SimulationState::Paused | SimulationState::Running
            ) || state.state == SimulationState::New && state.committing_cut.is_some())
            {
                return Err(state_error("inputs require a started healthy Simulation"));
            }
            if when < state.dispatch_time {
                return Err(admission("input time precedes the current dispatch cut"));
            }
            (when, state.validate_input.clone())
        };
        let input = validator(&endpoint, input)?;
        let mut state = shared.borrow_mut();
        if let Some(error) = &state.failure {
            return Err(error.clone());
        }
        if state.inputs.len() >= state.max_pending_inputs {
            drop(state);
            return Err(self.fail(Arc::new(Error::Capacity(
                "simulation input capacity exhausted".into(),
            ))));
        }
        let sequence = state.input_sequence;
        let Some(next) = sequence.checked_add(1) else {
            drop(state);
            return Err(self.fail(state_error("simulation input sequence overflow")));
        };
        state.input_sequence = next;
        state.inputs.push(Pending {
            when,
            sequence,
            endpoint,
            input,
        });
        Ok(())
    }

    fn emit(
        &self,
        node: &str,
        port: Option<&str>,
        kind: &str,
        payload: EventPayload<I, E>,
        time: EventTime,
    ) -> Result<Event<I, E>> {
        self.check_deadline()?;
        if kind.is_empty() {
            return Err(admission("event kind must be nonempty"));
        }
        let shared = self.active()?;
        let (event, observer) = {
            let mut shared = shared.borrow_mut();
            let session = shared
                .session
                .ok_or_else(|| state_error("observations require a session"))?;
            let Some(next) = shared.events_total.checked_add(1) else {
                drop(shared);
                return Err(self.fail(state_error("event sequence overflow")));
            };
            let event = Event {
                session_id: session.session_id,
                sequence: shared.events_total,
                source_node: node.to_owned(),
                source_port: port.map(str::to_owned),
                kind: kind.to_owned(),
                payload,
                source_time_ns: time.source_time_ns,
                mapped_time_ns: time.mapped_time_ns.unwrap_or(shared.dispatch_time),
                delivery_time_ns: time.delivery_time_ns.unwrap_or(shared.dispatch_time),
                quality: time.quality.unwrap_or(TimeQuality::Exact),
            };
            if shared.events.len() == shared.event_history {
                shared.events.pop_front();
                shared.events_dropped += 1;
            }
            shared.events.push_back(event.clone());
            shared.events_total = next;
            (event, shared.observer.clone())
        };
        if let Some(observer) = observer {
            if let Err(error) = observer(&event, self) {
                return Err(self.fail(error));
            }
            self.check_deadline()?;
        }
        Ok(event)
    }
}

#[derive(Clone)]
pub struct Context<I: Clone + 'static, E: Clone + 'static> {
    control: Control<I, E>,
    node: String,
    ports: Vec<String>,
}

impl<I: Clone + 'static, E: Clone + 'static> Context<I, E> {
    pub fn control(&self) -> Control<I, E> {
        self.control.clone()
    }
    pub fn node_id(&self) -> &str {
        &self.node
    }
    pub fn t_sim_ns(&self) -> Result<u64> {
        self.control.t_sim_ns()
    }
    pub fn not_before_ns(&self) -> Result<u64> {
        self.t_sim_ns()
    }
    pub fn session(&self) -> Result<Session> {
        self.control.session()
    }
    pub fn deadline(&self) -> Result<Option<Instant>> {
        self.control.deadline()
    }
    pub fn check_io(&self) -> Result<()> {
        self.control.check_io()
    }
    pub fn check_deadline(&self) -> Result<()> {
        self.control.check_deadline()
    }
    pub fn fail(&self, error: Failure) -> Failure {
        self.control.fail(error)
    }
    pub fn emit(
        &self,
        port: Option<&str>,
        kind: &str,
        payload: E,
        time: EventTime,
    ) -> Result<Event<I, E>> {
        if port.is_some_and(|port| !self.ports.iter().any(|known| known == port)) {
            return Err(admission("unknown observation port"));
        }
        self.control.emit(
            &self.node,
            port,
            kind,
            EventPayload::Observation(payload),
            time,
        )
    }
}

struct Owned<I: Clone + 'static, E: Clone + 'static> {
    node: usize,
    component: Box<dyn Component<I, E>>,
    context: Context<I, E>,
}

pub struct BuildContext<'a, O: 'static, I: Clone + 'static, E: Clone + 'static> {
    plan: &'a Plan<O, I, E>,
    owned: &'a mut Vec<Owned<I, E>>,
    attached: &'a mut BTreeSet<Endpoint>,
    node: usize,
    context: Context<I, E>,
}

impl<O: 'static, I: Clone + 'static, E: Clone + 'static> BuildContext<'_, O, I, E> {
    pub fn context(&self) -> Context<I, E> {
        self.context.clone()
    }

    pub fn peers(&self, port: &str) -> Result<&[Endpoint]> {
        self.plan.descriptions[self.node].port(port)?;
        let endpoint = Endpoint::new(&self.context.node, port)?;
        Ok(self.plan.peers.get(&endpoint).map_or(&[], Vec::as_slice))
    }

    pub fn attach<T: 'static, R: 'static>(&mut self, port: &str, endpoint: T) -> Result<R> {
        let name = Endpoint::new(&self.context.node, port)?;
        if self.attached.contains(&name) {
            return Err(admission("port is already attached"));
        }
        let descriptor = self.plan.descriptions[self.node].port(port)?.clone();
        let peers = self.peers(port)?;
        if peers.len() != 1 {
            return Err(admission("borrower requires exactly one owner peer"));
        }
        let peer = peers[0].clone();
        let owner_node = self.plan.names[&peer.node];
        if !self.plan.descriptions[owner_node].has(owner_capability(descriptor.category)) {
            return Err(admission(
                "peer lacks the required connection/binding owner capability",
            ));
        }
        let owner = self
            .owned
            .iter_mut()
            .find(|owner| owner.node == owner_node)
            .ok_or_else(|| state_error("connection owner must already be constructed"))?;
        let connection = owner
            .component
            .connection_owner()
            .ok_or_else(|| state_error("owner lacks attachment interface"))?
            .attach_port(Attachment {
                descriptor,
                endpoint_name: name.to_string(),
                owner_port: peer.port,
                endpoint: Box::new(endpoint),
                context: self.context.clone(),
            })?;
        let connection = connection
            .downcast::<R>()
            .map_err(|_| state_error("owner returned the wrong typed connection"))?;
        self.attached.insert(name);
        Ok(*connection)
    }
}

pub struct ComponentHandle<T> {
    identity: Weak<()>,
    generation: u64,
    node: usize,
    component: PhantomData<fn() -> T>,
}

impl<T> Clone for ComponentHandle<T> {
    fn clone(&self) -> Self {
        Self {
            identity: self.identity.clone(),
            generation: self.generation,
            node: self.node,
            component: PhantomData,
        }
    }
}

#[derive(Clone, Debug)]
pub struct AdvanceResult<E> {
    pub status: StepStatus,
    pub reached_time_ns: u64,
    pub requested_time_ns: u64,
    pub step_id: u64,
    pub substeps_completed: u64,
    pub components: BTreeMap<String, StepResult<E>>,
}

/// The sole owner of component instances and committed simulation time.
/// All callbacks run synchronously; native calls must enforce their own bounded
/// waits. Host deadlines are checked before and after each adapter boundary.
pub struct Simulation<O: 'static, I: Clone + 'static, E: Clone + 'static> {
    plan: Rc<Plan<O, I, E>>,
    options: SimulationOptions,
    shared: Rc<RefCell<Shared<I, E>>>,
    owned: Vec<Owned<I, E>>,
}

impl<O: 'static, I: Clone + 'static, E: Clone + 'static> Simulation<O, I, E> {
    pub fn new(
        topology: Topology<O>,
        registry: Registry<O, I, E>,
        options: SimulationOptions,
    ) -> Result<Self> {
        if options.quantum_ns == 0
            || options.startup_timeout.is_zero()
            || options
                .host_timeout
                .is_some_and(|duration| duration.is_zero())
            || options.max_pending_inputs == 0
            || options.dispatch_budget == 0
            || options.event_history == 0
        {
            return Err(admission(
                "simulation limits and host durations must be positive",
            ));
        }
        Session::new(
            options.initial_session.session_id,
            options.initial_session.t_sim_ns,
        )?;
        let plan = Rc::new(Plan::admit(topology, registry)?);
        let input_plan = plan.clone();
        let shared = Shared {
            identity: Rc::new(()),
            generation: 1,
            state: SimulationState::New,
            session: None,
            completed_time: 0,
            dispatch_time: 0,
            step_id: 0,
            phase: "new",
            failure: None,
            notes: Vec::new(),
            closing: false,
            stepping: false,
            checking_health: false,
            pause_requested: false,
            committing_cut: None,
            deadline: None,
            host_deadline: None,
            borrowed_deadline: false,
            health: Vec::new(),
            cancellation: options.cancellation.clone(),
            inputs: BinaryHeap::new(),
            input_sequence: 0,
            max_pending_inputs: options.max_pending_inputs,
            validate_input: Rc::new(move |endpoint, value| {
                input_plan.validate_input(endpoint, value)
            }),
            events: VecDeque::new(),
            event_history: options.event_history,
            events_total: 0,
            events_dropped: 0,
            observer: None,
        };
        Ok(Self {
            plan,
            options,
            shared: Rc::new(RefCell::new(shared)),
            owned: Vec::new(),
        })
    }

    pub fn state(&self) -> SimulationState {
        self.shared.borrow().state
    }
    pub fn session(&self) -> Option<Session> {
        self.shared.borrow().session
    }
    pub fn t_sim_ns(&self) -> u64 {
        self.shared.borrow().completed_time
    }
    pub fn step_id(&self) -> u64 {
        self.shared.borrow().step_id
    }
    pub fn failure(&self) -> Option<Failure> {
        self.shared.borrow().failure.clone()
    }
    pub fn failure_notes(&self) -> Vec<String> {
        self.shared.borrow().notes.clone()
    }
    pub fn events(&self) -> Vec<Event<I, E>> {
        self.shared.borrow().events.iter().cloned().collect()
    }
    pub fn events_total(&self) -> u64 {
        self.shared.borrow().events_total
    }
    pub fn events_dropped(&self) -> u64 {
        self.shared.borrow().events_dropped
    }
    pub fn validate(&self) -> &[NodeDescription] {
        &self.plan.descriptions
    }
    /// Reuse the admitted factory's pure external-input boundary without
    /// starting resources or placing the stimulus in a queue.
    pub fn validate_input(&self, endpoint: &Endpoint, input: I) -> Result<I> {
        self.plan.validate_input(endpoint, input)
    }
    pub fn topology(&self) -> &Topology<O> {
        &self.plan.topology
    }
    pub fn control(&self) -> Control<I, E> {
        Control {
            shared: Rc::downgrade(&self.shared),
            generation: self.shared.borrow().generation,
        }
    }
    pub fn set_observer(&mut self, observer: Option<Observer<I, E>>) {
        self.shared.borrow_mut().observer = observer;
    }

    pub fn handle<T: Component<I, E>>(&self, node: &str) -> Result<ComponentHandle<T>> {
        let node = *self
            .plan
            .names
            .get(node)
            .ok_or_else(|| admission("unknown component node"))?;
        if !self
            .owned
            .iter()
            .any(|owned| owned.node == node && (owned.component.as_ref() as &dyn Any).is::<T>())
        {
            return Err(state_error(
                "component is unavailable or has a different concrete type",
            ));
        }
        let shared = self.shared.borrow();
        Ok(ComponentHandle {
            identity: Rc::downgrade(&shared.identity),
            generation: shared.generation,
            node,
            component: PhantomData,
        })
    }

    fn check_handle<T>(&self, handle: &ComponentHandle<T>) -> Result<()> {
        let shared = self.shared.borrow();
        if !Weak::ptr_eq(&handle.identity, &Rc::downgrade(&shared.identity))
            || handle.generation != shared.generation
            || shared.state == SimulationState::Closed
        {
            return Err(state_error(
                "component handle belongs to a different or closed session",
            ));
        }
        Ok(())
    }

    pub fn component<T: Component<I, E>>(&self, handle: &ComponentHandle<T>) -> Result<&T> {
        self.check_handle(handle)?;
        self.owned
            .iter()
            .find(|owned| owned.node == handle.node)
            .and_then(|owned| (owned.component.as_ref() as &dyn Any).downcast_ref::<T>())
            .ok_or_else(|| state_error("typed component is unavailable"))
    }

    pub fn with_component<T: Component<I, E>, R>(
        &mut self,
        handle: &ComponentHandle<T>,
        operation: impl FnOnce(&mut T, &Context<I, E>) -> Result<R>,
    ) -> Result<R> {
        self.check_handle(handle)?;
        let control = self.control();
        control.check_io()?;
        control.check_deadline()?;
        let owned = self
            .owned
            .iter_mut()
            .find(|owned| owned.node == handle.node)
            .ok_or_else(|| state_error("typed component is unavailable"))?;
        let component = (owned.component.as_mut() as &mut dyn Any)
            .downcast_mut::<T>()
            .ok_or_else(|| state_error("typed component is unavailable"))?;
        let result = operation(component, &owned.context);
        if let Some(failure) = self.failure() {
            return Err(failure);
        }
        if result.is_ok() {
            control.check_deadline()?;
        }
        result
    }

    fn phase(&self, phase: &'static str) {
        self.shared.borrow_mut().phase = phase;
    }

    fn admit_component(
        description: &NodeDescription,
        component: &mut dyn Component<I, E>,
    ) -> Result<Option<HealthCheck>> {
        if description.has(Capability::Machine) && component.machine().is_none()
            || description.has(Capability::Step) && component.stepper().is_none()
            || description.has(Capability::Input) && component.input().is_none()
            || description.has(Capability::InputCommit) && component.input_commit().is_none()
            || description.has(Capability::Sample) && component.sampler().is_none()
            || (description.has(Capability::Connection)
                || description.has(Capability::BindingOwner))
                && component.connection_owner().is_none()
        {
            return Err(state_error(
                "returned component lacks a declared capability interface",
            ));
        }
        if description.has(Capability::Dispatch) {
            let dispatcher = component
                .dispatcher()
                .ok_or_else(|| state_error("component lacks dispatch interface"))?;
            if dispatcher.dispatch_budget() == 0 {
                return Err(admission("dispatcher budget must be positive"));
            }
            dispatcher.next_due_ns()?;
        }
        if description.has(Capability::Health) {
            return component
                .health_check()
                .map(Some)
                .ok_or_else(|| state_error("component lacks health check"));
        }
        Ok(None)
    }

    fn create_resources(&mut self) -> Result<()> {
        self.phase("creating");
        let plan = self.plan.clone();
        let control = self.control();
        let mut attached = BTreeSet::new();
        for &node in &plan.construction {
            control.check_deadline()?;
            let context = Context {
                control: control.clone(),
                node: plan.topology.nodes[node].name.clone(),
                ports: plan.descriptions[node]
                    .ports
                    .iter()
                    .map(|port| port.name.clone())
                    .collect(),
            };
            let mut build = BuildContext {
                plan: &plan,
                owned: &mut self.owned,
                attached: &mut attached,
                node,
                context: context.clone(),
            };
            let (_, factory) = plan.registry.get(&plan.topology.nodes[node].kind)?;
            let component = factory.create(&plan.topology.nodes[node], &mut build)?;
            self.owned.push(Owned {
                node,
                component,
                context,
            });
            control.check_deadline()?;
            let health = Self::admit_component(
                &plan.descriptions[node],
                self.owned
                    .last_mut()
                    .expect("registered owner")
                    .component
                    .as_mut(),
            )?;
            if let Some(health) = health {
                self.shared.borrow_mut().health.push(health);
            }
        }
        for link in &plan.topology.links {
            let index = plan.names[&link.source.node];
            let description = &plan.descriptions[index];
            let owner = owner_capability(description.port(&link.source.port)?.category);
            let borrower = if description.has(owner) {
                &link.target
            } else {
                &link.source
            };
            if !attached.contains(borrower) {
                return Err(admission(format!("factory did not attach {borrower}")));
            }
        }
        self.phase("initializing");
        let session = self.session().expect("started session");
        for owned in &mut self.owned {
            control.check_deadline()?;
            owned.component.reset(session, &owned.context)?;
            control.check_deadline()?;
        }
        for owned in &mut self.owned {
            control.check_deadline()?;
            owned.component.start(&owned.context)?;
            control.check_deadline()?;
        }
        self.phase("initial sample");
        self.sample(self.t_sim_ns())?;
        self.phase("initial delivery");
        self.settle(self.t_sim_ns())
    }

    pub fn start(&mut self, deadline: Option<Instant>) -> Result<()> {
        if self.state() != SimulationState::New || self.shared.borrow().phase != "new" {
            return Err(state_error("start requires a new Simulation"));
        }
        let begun = Instant::now();
        let host_timeout = self.options.host_timeout.unwrap_or_else(|| {
            Duration::from_secs(if self.options.mode == RunMode::Interactive {
                300
            } else {
                30
            })
        });
        let host = match deadline {
            Some(deadline) => deadline,
            None => begun
                .checked_add(host_timeout)
                .ok_or_else(|| admission("host timeout overflows Instant"))?,
        };
        let startup = if deadline.is_some() {
            host
        } else {
            host.min(
                begun
                    .checked_add(self.options.startup_timeout)
                    .ok_or_else(|| admission("startup timeout overflows Instant"))?,
            )
        };
        {
            let mut state = self.shared.borrow_mut();
            state.deadline = Some(startup);
            state.host_deadline = Some(host);
            state.borrowed_deadline = deadline.is_some();
            state.session = Some(self.options.initial_session);
            state.completed_time = self.options.initial_session.t_sim_ns;
            state.dispatch_time = state.completed_time;
        }
        let control = self.control();
        if let Err(error) = self
            .create_resources()
            .and_then(|()| control.check_deadline())
        {
            let primary = control.fail(error);
            self.close_owned(Some(primary.clone()));
            self.phase("failed");
            return Err(primary);
        }
        let mut state = self.shared.borrow_mut();
        state.deadline = state.host_deadline;
        state.phase = "paused";
        state.state = SimulationState::Paused;
        Ok(())
    }

    fn operation_deadline(&self, deadline: Option<Instant>) -> Result<()> {
        let mut state = self.shared.borrow_mut();
        if state.borrowed_deadline {
            let deadline = deadline.ok_or_else(|| {
                admission("borrowed deadline requires a deadline for every operation")
            })?;
            state.deadline = Some(deadline);
            state.host_deadline = Some(deadline);
        } else if deadline.is_some() {
            return Err(admission(
                "absolute deadlines require start with a borrowed deadline",
            ));
        }
        Ok(())
    }

    pub fn schedule_input(&self, endpoint: Endpoint, input: I, at_ns: Option<u64>) -> Result<()> {
        self.control().schedule_input(endpoint, input, at_ns)
    }
    pub fn pause(&self) -> Result<()> {
        self.control().pause()
    }
    pub fn check_deadline(&self) -> Result<()> {
        if !matches!(
            self.state(),
            SimulationState::Paused | SimulationState::Running
        ) {
            return Err(state_error(
                "deadline checks require a started healthy Simulation",
            ));
        }
        self.control().check_deadline()
    }

    fn sample(&mut self, cut: u64) -> Result<()> {
        let control = self.control();
        for owned in &mut self.owned {
            if self.plan.descriptions[owned.node].has(Capability::Sample) {
                control.check_deadline()?;
                owned
                    .component
                    .sampler()
                    .ok_or_else(|| state_error("missing sample interface"))?
                    .sample(cut, &owned.context)?;
                control.check_deadline()?;
            }
        }
        Ok(())
    }

    fn dispatcher_due(&mut self, position: usize) -> Result<Option<u64>> {
        self.owned[position]
            .component
            .dispatcher()
            .ok_or_else(|| state_error("missing dispatcher interface"))?
            .next_due_ns()
    }

    fn settle(&mut self, cut: u64) -> Result<()> {
        self.shared.borrow_mut().dispatch_time = cut;
        let control = self.control();
        let dispatchers: Vec<_> = self
            .owned
            .iter()
            .enumerate()
            .filter(|(_, owned)| self.plan.descriptions[owned.node].has(Capability::Dispatch))
            .map(|(index, _)| index)
            .collect();
        let mut budgets = Vec::new();
        for &index in &dispatchers {
            let budget = self.owned[index]
                .component
                .dispatcher()
                .ok_or_else(|| state_error("missing dispatcher interface"))?
                .dispatch_budget();
            if budget == 0 {
                return Err(admission("dispatcher budget must be positive"));
            }
            budgets.push(budget);
        }
        let mut remaining = self.options.dispatch_budget;
        loop {
            control.check_deadline()?;
            loop {
                let due = self
                    .shared
                    .borrow()
                    .inputs
                    .peek()
                    .is_some_and(|input| input.when <= cut);
                if !due {
                    break;
                }
                if remaining == 0 {
                    return Err(state_error(
                        "input dispatch budget exhausted (possible event loop)",
                    ));
                }
                let pending = self.shared.borrow_mut().inputs.pop().expect("due input");
                let node = self.plan.names[&pending.endpoint.node];
                let owned = self
                    .owned
                    .iter_mut()
                    .find(|owned| owned.node == node)
                    .ok_or_else(|| state_error("input owner is unavailable"))?;
                owned
                    .component
                    .input()
                    .ok_or_else(|| state_error("missing input interface"))?
                    .input(
                        &pending.endpoint.port,
                        pending.input.clone(),
                        &owned.context,
                    )?;
                control.emit(
                    &pending.endpoint.node,
                    Some(&pending.endpoint.port),
                    "input.delivered",
                    EventPayload::Input(pending.input),
                    EventTime {
                        source_time_ns: Some(pending.when),
                        mapped_time_ns: Some(pending.when),
                        delivery_time_ns: Some(cut),
                        quality: None,
                    },
                )?;
                control.check_deadline()?;
                remaining -= 1;
            }
            for (position, &index) in dispatchers.iter().enumerate() {
                control.check_deadline()?;
                let due = self.dispatcher_due(index)?;
                let budget = remaining.min(budgets[position]);
                if budget == 0 && due.is_some_and(|due| due <= cut) {
                    return Err(state_error(
                        "dispatcher budget exhausted (possible event loop)",
                    ));
                }
                let owned = &mut self.owned[index];
                let dispatched = owned
                    .component
                    .dispatcher()
                    .ok_or_else(|| state_error("missing dispatcher interface"))?
                    .dispatch(cut, budget, &owned.context)?;
                if dispatched > budget {
                    return Err(state_error(
                        "dispatcher returned a count exceeding its budget",
                    ));
                }
                remaining -= dispatched;
                budgets[position] -= dispatched;
                let after = self.dispatcher_due(index)?;
                if dispatched == 0
                    && (due.is_some_and(|due| due <= cut) || after.is_some_and(|due| due <= cut))
                {
                    return Err(state_error("dispatcher made no progress on due work"));
                }
                control.check_deadline()?;
            }
            let mut due = self
                .shared
                .borrow()
                .inputs
                .peek()
                .is_some_and(|input| input.when <= cut);
            for &index in &dispatchers {
                due |= self.dispatcher_due(index)?.is_some_and(|due| due <= cut);
            }
            if !due {
                return self.commit_inputs(cut, &dispatchers);
            }
        }
    }

    fn commit_inputs(&mut self, cut: u64, dispatchers: &[usize]) -> Result<()> {
        let previous = self.shared.borrow().phase;
        {
            let mut state = self.shared.borrow_mut();
            state.phase = "input commit";
            state.committing_cut = Some(cut);
        }
        let control = self.control();
        let result = (|| {
            for index in 0..self.owned.len() {
                if self.plan.descriptions[self.owned[index].node].has(Capability::InputCommit) {
                    control.check_deadline()?;
                    let owned = &mut self.owned[index];
                    owned
                        .component
                        .input_commit()
                        .ok_or_else(|| state_error("missing input commit interface"))?
                        .commit_inputs(cut, &owned.context)?;
                    control.check_deadline()?;
                    for &dispatcher in dispatchers {
                        if self
                            .dispatcher_due(dispatcher)?
                            .is_some_and(|due| due <= cut)
                        {
                            return Err(state_error(
                                "input commit reopened a dispatcher at its completed cut",
                            ));
                        }
                    }
                }
            }
            Ok(())
        })();
        let mut state = self.shared.borrow_mut();
        state.committing_cut = None;
        state.phase = previous;
        result
    }

    fn reached(result: &StepResult<E>, target: u64) -> Result<()> {
        if result.status != StepStatus::Ok || result.reached_time_ns != target {
            return Err(state_error(
                "component did not reach the exact requested target",
            ));
        }
        Ok(())
    }

    fn interval(&mut self, target: u64) -> Result<BTreeMap<String, StepResult<E>>> {
        let current = self.shared.borrow();
        let step = StepContext::new(
            current.session.expect("started session").session_id,
            current
                .step_id
                .checked_add(1)
                .ok_or_else(|| state_error("step ID overflow"))?,
            target,
            target - current.completed_time,
        )?;
        drop(current);
        self.shared.borrow_mut().dispatch_time = target;
        let control = self.control();
        let mut results = BTreeMap::new();
        for phase in [
            "machine preflight",
            "machine request",
            "machine completion",
            "component step",
        ] {
            self.phase(phase);
            for owned in &mut self.owned {
                let capability = if phase == "component step" {
                    Capability::Step
                } else {
                    Capability::Machine
                };
                if !self.plan.descriptions[owned.node].has(capability) {
                    continue;
                }
                control.check_deadline()?;
                let result = if phase == "component step" {
                    Some(
                        owned
                            .component
                            .stepper()
                            .ok_or_else(|| state_error("missing step interface"))?
                            .step(&step, &owned.context)?,
                    )
                } else {
                    let machine = owned
                        .component
                        .machine()
                        .ok_or_else(|| state_error("missing machine interface"))?;
                    match phase {
                        "machine preflight" => {
                            machine.prepare_step(&step, &owned.context)?;
                            None
                        }
                        "machine request" => {
                            machine.request_step(&step, &owned.context)?;
                            None
                        }
                        _ => Some(machine.finish_step(&step, &owned.context)?),
                    }
                };
                if let Some(result) = result {
                    Self::reached(&result, target)?;
                    results.insert(owned.context.node.clone(), result);
                }
                control.check_deadline()?;
            }
        }
        self.phase("target sample");
        self.sample(target)?;
        self.phase("target delivery");
        self.settle(target)?;
        control.check_deadline()?;
        let mut state = self.shared.borrow_mut();
        state.completed_time = target;
        state.step_id = step.step_id;
        Ok(results)
    }

    fn advance(&mut self, target: u64) -> Result<AdvanceResult<E>> {
        {
            let mut state = self.shared.borrow_mut();
            if state.stepping {
                return Err(state_error("Simulation execution cannot be reentered"));
            }
            state.pause_requested = false;
            state.state = SimulationState::Running;
            state.stepping = true;
        }
        let control = self.control();
        let result = (|| {
            self.phase("start delivery");
            self.settle(self.t_sim_ns())?;
            let mut completed = 0;
            let mut results = BTreeMap::new();
            while self.t_sim_ns() < target && !self.shared.borrow().pause_requested {
                let current = self.t_sim_ns();
                let mut cut = target.min(current.saturating_add(self.options.quantum_ns));
                if let Some(input) = self.shared.borrow().inputs.peek() {
                    cut = cut.min(input.when);
                }
                for index in 0..self.owned.len() {
                    if self.plan.descriptions[self.owned[index].node].has(Capability::Dispatch) {
                        if let Some(due) = self.dispatcher_due(index)? {
                            if due <= current {
                                return Err(state_error(
                                    "dispatcher reported unsettled work after settling a cut",
                                ));
                            }
                            cut = cut.min(due);
                        }
                    }
                }
                results = self.interval(cut)?;
                completed += 1;
            }
            control.check_deadline()?;
            Ok(AdvanceResult {
                status: if self.t_sim_ns() < target {
                    StepStatus::Paused
                } else {
                    StepStatus::Ok
                },
                reached_time_ns: self.t_sim_ns(),
                requested_time_ns: target,
                step_id: self.step_id(),
                substeps_completed: completed,
                components: results,
            })
        })();
        if let Err(error) = &result {
            let primary = control.fail(error.clone());
            let mut state = self.shared.borrow_mut();
            let note = format!(
                "failed during {}, next step {}; completed time remains {}",
                state.phase,
                state.step_id.saturating_add(1),
                state.completed_time
            );
            state.notes.push(note);
            state.stepping = false;
            return Err(primary);
        }
        let mut state = self.shared.borrow_mut();
        state.stepping = false;
        if state.state == SimulationState::Running {
            state.state = SimulationState::Paused;
            state.phase = "paused";
        }
        result
    }

    pub fn step(
        &mut self,
        dt_ns: Option<u64>,
        deadline: Option<Instant>,
    ) -> Result<AdvanceResult<E>> {
        if self.state() != SimulationState::Paused {
            return Err(state_error("step requires a paused healthy Simulation"));
        }
        let dt = dt_ns.unwrap_or(self.options.quantum_ns);
        if dt == 0 {
            return Err(admission("step duration must be positive"));
        }
        let target = self
            .t_sim_ns()
            .checked_add(dt)
            .ok_or_else(|| admission("simulation target overflows unsigned 64-bit time"))?;
        self.operation_deadline(deadline)?;
        self.advance(target)
    }

    pub fn run_until(
        &mut self,
        target: u64,
        deadline: Option<Instant>,
    ) -> Result<AdvanceResult<E>> {
        if self.state() != SimulationState::Paused {
            return Err(state_error("run requires a paused healthy Simulation"));
        }
        if target < self.t_sim_ns() {
            return Err(admission("run target precedes completed simulation time"));
        }
        self.operation_deadline(deadline)?;
        self.advance(target)
    }

    fn close_owned(&mut self, mut primary: Option<Failure>) -> Option<Failure> {
        {
            let mut state = self.shared.borrow_mut();
            state.closing = true;
            state.health.clear();
        }
        while let Some(mut owned) = self.owned.pop() {
            if let Err(error) = owned.component.close() {
                if primary.is_none() {
                    primary = Some(error);
                } else {
                    self.shared.borrow_mut().notes.push(format!(
                        "component {} cleanup failed: {error}",
                        owned.context.node
                    ));
                }
            }
        }
        self.shared.borrow_mut().closing = false;
        primary
    }

    pub fn reset(&mut self, deadline: Option<Instant>) -> Result<()> {
        if self.state() != SimulationState::Paused || self.shared.borrow().stepping {
            return Err(state_error("reset requires a paused healthy Simulation"));
        }
        let old = self.session().expect("started session");
        let session = Session::new(
            old.session_id
                .checked_add(1)
                .ok_or_else(|| admission("session ID overflow"))?,
            self.options.initial_session.t_sim_ns,
        )?;
        let generation = self
            .shared
            .borrow()
            .generation
            .checked_add(1)
            .ok_or_else(|| admission("session generation overflow"))?;
        let startup = Instant::now()
            .checked_add(self.options.startup_timeout)
            .ok_or_else(|| admission("startup timeout overflows Instant"))?;
        self.operation_deadline(deadline)?;
        self.check_deadline()?;
        self.shared.borrow_mut().state = SimulationState::Running;
        if let Some(error) = self.close_owned(None) {
            return Err(self.control().fail(error));
        }
        {
            let mut state = self.shared.borrow_mut();
            state.inputs.clear();
            state.step_id = 0;
            state.generation = generation;
            state.session = Some(session);
            state.completed_time = session.t_sim_ns;
            state.dispatch_time = session.t_sim_ns;
            if !state.borrowed_deadline {
                state.deadline = Some(state.host_deadline.expect("active deadline").min(startup));
            }
        }
        let control = self.control();
        if let Err(error) = self
            .create_resources()
            .and_then(|()| control.check_deadline())
        {
            let primary = control.fail(error);
            self.close_owned(Some(primary.clone()));
            self.phase("failed");
            return Err(primary);
        }
        let mut state = self.shared.borrow_mut();
        state.deadline = state.host_deadline;
        state.state = SimulationState::Paused;
        state.phase = "paused";
        Ok(())
    }

    pub fn close(&mut self) -> Result<()> {
        if self.state() == SimulationState::Closed {
            return Ok(());
        }
        let state = self.shared.borrow();
        if state.stepping
            || state.closing
            || state.committing_cut.is_some()
            || matches!(
                state.phase,
                "creating" | "initializing" | "initial sample" | "initial delivery"
            )
        {
            return Err(state_error(
                "close cannot interrupt active construction/execution/cleanup",
            ));
        }
        drop(state);
        let mut primary = None;
        if matches!(
            self.state(),
            SimulationState::Paused | SimulationState::Running
        ) {
            if let Err(error) = self.control().check_deadline() {
                primary = Some(error);
            }
        }
        {
            let mut state = self.shared.borrow_mut();
            state.state = SimulationState::Closed;
            state.phase = "cleanup";
        }
        primary = self.close_owned(primary);
        self.shared.borrow_mut().inputs.clear();
        if let Some(primary) = primary {
            if let Some(first) = self.failure() {
                if !Arc::ptr_eq(&first, &primary) {
                    self.shared
                        .borrow_mut()
                        .notes
                        .push(format!("simulation cleanup failed: {primary}"));
                }
                return Err(first);
            }
            return Err(primary);
        }
        Ok(())
    }
}

impl<O: 'static, I: Clone + 'static, E: Clone + 'static> Drop for Simulation<O, I, E> {
    fn drop(&mut self) {
        if !self.owned.is_empty() {
            self.shared.borrow_mut().state = SimulationState::Closed;
            self.close_owned(None);
        }
    }
}
