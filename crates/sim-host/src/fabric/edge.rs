//! Explicit producer timestamps, preserved individually without sampling GPIO.

pub use super::sampled::NetOptions as EdgeNetOptions;
use crate::runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry};
use sim_core::{
    Attachment, BuildContext, Capability, Component, ConnectionOwner, Direction, Dispatcher,
    Endpoint, Error, EventTime, Factory, Failure, NodeDescription, NodeSpec, PortCategory,
    PortSpec, Result, Session, TimeQuality, Topology,
};
use std::{
    any::Any,
    cell::RefCell,
    collections::VecDeque,
    rc::{Rc, Weak},
    sync::Arc,
};

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DigitalEdge {
    pub level: bool,
    pub source_time_ns: u64,
    pub mapped_time_ns: u64,
}
pub type EdgeSink = Rc<dyn Fn(DigitalEdge) -> Result<()>>;
pub enum EdgeAttachment {
    Source,
    Sink(EdgeSink),
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EdgeDelivery {
    pub source: String,
    pub destination: String,
    pub edge: DigitalEdge,
    pub sequence: u64,
    pub due_ns: u64,
    pub cut_ns: u64,
}
struct Broadcast {
    due_ns: u64,
    sequence: u64,
    edge: DigitalEdge,
}
struct State {
    options: EdgeNetOptions,
    source: Option<String>,
    sinks: Vec<(String, EdgeSink)>,
    queue: VecDeque<Broadcast>,
    last_edge: Option<DigitalEdge>,
    sequence: u64,
    time_ns: u64,
    initialized: bool,
    closed: bool,
    busy: bool,
    failure: Option<Failure>,
}
impl State {
    fn check(&self) -> Result<()> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.closed || !self.initialized {
            return Err(state_error("digital-edge requires a healthy open session"));
        }
        Ok(())
    }
    fn freeze(&mut self, error: Failure) -> Failure {
        self.failure.get_or_insert(error).clone()
    }
}
/// Borrowed output; it never keeps the connection owner alive.
#[derive(Clone)]
pub struct EdgeSender {
    state: Weak<RefCell<State>>,
}
impl EdgeSender {
    pub fn send(&self, edge: DigitalEdge, floor_ns: u64) -> Result<()> {
        let state = self
            .state
            .upgrade()
            .ok_or_else(|| state_error("digital-edge owner was dropped"))?;
        let mut state = state.borrow_mut();
        state.check()?;
        let result = (|| {
            if state.last_edge.is_some_and(|last| {
                edge.source_time_ns < last.source_time_ns
                    || edge.mapped_time_ns < last.mapped_time_ns
            }) {
                return Err(admission(
                    "digital-edge source and mapped times must not decrease",
                ));
            }
            if state.queue.len() >= state.options.max_pending {
                return Err(Arc::new(Error::Capacity(
                    "digital-edge pending capacity exhausted".into(),
                )) as Failure);
            }
            let next = state
                .sequence
                .checked_add(1)
                .ok_or_else(|| state_error("digital-edge sequence exhausted"))?;
            let due_ns = edge.mapped_time_ns.max(floor_ns).max(state.time_ns);
            let sequence = state.sequence;
            state.queue.push_back(Broadcast {
                due_ns,
                sequence,
                edge,
            });
            state.sequence = next;
            state.last_edge = Some(edge);
            Ok(())
        })();
        result.map_err(|error| state.freeze(error))
    }
}
/// Sole edge queue. No RefCell borrow is held across an endpoint or observer callback.
pub struct DigitalEdgeNet {
    state: Rc<RefCell<State>>,
}
impl DigitalEdgeNet {
    pub fn new(options: EdgeNetOptions) -> Result<Self> {
        options.validate()?;
        Ok(Self {
            state: Rc::new(RefCell::new(State {
                options,
                source: None,
                sinks: vec![],
                queue: VecDeque::new(),
                last_edge: None,
                sequence: 0,
                time_ns: 0,
                initialized: false,
                closed: false,
                busy: false,
                failure: None,
            })),
        })
    }
    pub fn attach(&mut self, name: String, endpoint: EdgeAttachment) -> Result<Option<EdgeSender>> {
        let mut state = self.state.borrow_mut();
        if state.closed || state.initialized {
            return Err(state_error("digital-edge wiring must precede reset"));
        }
        Endpoint::parse(&name)?;
        if state.source.as_ref() == Some(&name)
            || state.sinks.iter().any(|(known, _)| known == &name)
        {
            return Err(admission("digital-edge endpoint is already attached"));
        }
        if state.sinks.len() + usize::from(state.source.is_some()) >= 256 {
            return Err(Arc::new(Error::Capacity(
                "digital-edge endpoint capacity exhausted".into(),
            )));
        }
        match endpoint {
            EdgeAttachment::Source => {
                if state.source.is_some() {
                    return Err(admission("digital-edge net permits one source"));
                }
                state.source = Some(name);
                Ok(Some(EdgeSender {
                    state: Rc::downgrade(&self.state),
                }))
            }
            EdgeAttachment::Sink(receive) => {
                if state
                    .sinks
                    .iter()
                    .any(|(_, known)| Rc::ptr_eq(known, &receive))
                {
                    return Err(admission("digital-edge endpoint is already attached"));
                }
                state.sinks.push((name, receive));
                Ok(None)
            }
        }
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        let mut state = self.state.borrow_mut();
        if state.closed || state.busy {
            return Err(state_error(
                "digital-edge reset requires an open idle owner",
            ));
        }
        Session::new(session.session_id, session.t_sim_ns)?;
        if state.source.is_none() || state.sinks.is_empty() {
            return Err(admission(
                "digital-edge requires one source and at least one sink",
            ));
        }
        state.queue.clear();
        state.last_edge = None;
        state.sequence = 0;
        state.time_ns = session.t_sim_ns;
        state.failure = None;
        state.initialized = true;
        Ok(())
    }
    pub fn pending(&self) -> usize {
        self.state.borrow().queue.len()
    }
    pub fn failure(&self) -> Option<Failure> {
        self.state.borrow().failure.clone()
    }
    pub fn dispatch_budget(&self) -> usize {
        self.state.borrow().options.dispatch_budget
    }
    pub fn next_due_ns(&self) -> Result<Option<u64>> {
        let state = self.state.borrow();
        if let Some(error) = &state.failure {
            return Err(error.clone());
        }
        Ok(state.queue.front().map(|item| item.due_ns))
    }
    pub fn dispatch(
        &mut self,
        cut: u64,
        budget: usize,
        observe: &mut impl FnMut(EdgeDelivery) -> Result<()>,
    ) -> Result<usize> {
        let (source, sinks, budget) = {
            let mut state = self.state.borrow_mut();
            state.check()?;
            if state.busy {
                return Err(state_error("digital-edge dispatch cannot be reentered"));
            }
            if cut < state.time_ns {
                return Err(admission("digital-edge cut must not decrease"));
            }
            state.time_ns = cut;
            state.busy = true;
            (
                state.source.clone().expect("reset admitted source"),
                state.sinks.clone(),
                budget.min(state.options.dispatch_budget),
            )
        };
        let result = (|| {
            let mut count = 0;
            while count < budget {
                let item = {
                    let mut state = self.state.borrow_mut();
                    state.check()?;
                    if state.queue.front().is_none_or(|item| item.due_ns > cut) {
                        break;
                    }
                    state.queue.pop_front().expect("front exists")
                };
                count += 1;
                for (destination, receive) in &sinks {
                    let result = receive(item.edge);
                    self.state.borrow().check()?;
                    result?;
                    let result = observe(EdgeDelivery {
                        source: source.clone(),
                        destination: destination.clone(),
                        edge: item.edge,
                        sequence: item.sequence,
                        due_ns: item.due_ns,
                        cut_ns: cut,
                    });
                    self.state.borrow().check()?;
                    result?;
                }
            }
            Ok(count)
        })();
        let mut state = self.state.borrow_mut();
        state.busy = false;
        result.map_err(|error| state.freeze(error))
    }
    pub fn close(&mut self) -> Result<()> {
        let mut state = self.state.borrow_mut();
        if state.busy {
            return Err(state_error("digital-edge cannot close during dispatch"));
        }
        state.closed = true;
        state.queue.clear();
        state.source = None;
        state.sinks.clear();
        Ok(())
    }
}
#[derive(Clone)]
pub struct EdgeOutput {
    sender: EdgeSender,
    context: HostContext,
}
impl EdgeOutput {
    pub fn send(&self, edge: DigitalEdge) -> Result<()> {
        self.context.check_io()?;
        self.context.check_deadline()?;
        self.sender
            .send(edge, self.context.not_before_ns()?)
            .map_err(|error| self.context.fail(error))
    }
}
pub struct DigitalEdgeNetwork {
    net: DigitalEdgeNet,
}
impl DigitalEdgeNetwork {
    pub fn pending(&self) -> usize {
        self.net.pending()
    }
}
impl ConnectionOwner<HostInput, HostEvent> for DigitalEdgeNetwork {
    fn attach_port(
        &mut self,
        attachment: Attachment<HostInput, HostEvent>,
    ) -> Result<Box<dyn Any>> {
        let descriptor = &attachment.descriptor;
        validate_port(&attachment.owner_port, descriptor)?;
        let endpoint = *attachment
            .endpoint
            .downcast::<EdgeAttachment>()
            .map_err(|_| admission("digital-edge requires a typed edge attachment"))?;
        let context = attachment.context;
        match (descriptor.direction, endpoint) {
            (Direction::Out, EdgeAttachment::Source) => {
                let sender = self
                    .net
                    .attach(attachment.endpoint_name, EdgeAttachment::Source)?
                    .expect("source returns output");
                Ok(Box::new(EdgeOutput { sender, context }))
            }
            (Direction::In, EdgeAttachment::Sink(receive)) => {
                self.net.attach(
                    attachment.endpoint_name,
                    EdgeAttachment::Sink(Rc::new(move |edge| {
                        context.check_io()?;
                        context.check_deadline()?;
                        let result = receive(edge);
                        context.check_io()?;
                        context.check_deadline()?;
                        result.map_err(|error| context.fail(error))
                    })),
                )?;
                Ok(Box::new(()))
            }
            _ => Err(admission(
                "digital-edge attachment must match port direction",
            )),
        }
    }
}
impl Dispatcher<HostInput, HostEvent> for DigitalEdgeNetwork {
    fn dispatch_budget(&self) -> usize {
        self.net.dispatch_budget()
    }
    fn next_due_ns(&self) -> Result<Option<u64>> {
        self.net.next_due_ns()
    }
    fn dispatch(&mut self, cut: u64, budget: usize, context: &HostContext) -> Result<usize> {
        context.check_io()?;
        self.net
            .dispatch(cut, budget, &mut |event| {
                context.emit(
                    Some("signal"),
                    "net.digital-edge.delivery",
                    HostEvent::DigitalEdgeDelivery {
                        source: event.source,
                        destination: event.destination,
                        level: event.edge.level,
                        sequence: event.sequence,
                        due_ns: event.due_ns,
                    },
                    EventTime {
                        source_time_ns: Some(event.edge.source_time_ns),
                        mapped_time_ns: Some(event.edge.mapped_time_ns),
                        delivery_time_ns: Some(cut),
                        quality: Some(TimeQuality::Exact),
                    },
                )?;
                context.check_io()?;
                context.check_deadline()
            })
            .map_err(|error| context.fail(error))
    }
}
impl Component<HostInput, HostEvent> for DigitalEdgeNetwork {
    fn close(&mut self) -> Result<()> {
        self.net.close()
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        self.net.reset(session)
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<HostInput, HostEvent>> {
        Some(self)
    }
    fn connection_owner(&mut self) -> Option<&mut dyn ConnectionOwner<HostInput, HostEvent>> {
        Some(self)
    }
}
fn validate_port(owner_port: &str, port: &PortSpec) -> Result<()> {
    if owner_port != "signal"
        || port.protocol != "digital-edge"
        || port.category != PortCategory::Signal
        || !matches!(port.direction, Direction::In | Direction::Out)
    {
        return Err(admission(
            "digital-edge endpoints must be input or output signals",
        ));
    }
    if port.direction == Direction::Out && !port.capabilities.contains("event-output") {
        return Err(admission(
            "digital-edge source requires event-output capability",
        ));
    }
    Ok(())
}
pub struct DigitalEdgeNetFactory;
impl Factory<HostOptions, HostInput, HostEvent> for DigitalEdgeNetFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::DigitalEdgeNet(options) = &node.options else {
            return Err(admission("net.digital-edge requires edge options"));
        };
        options.validate()?;
        let mut signal = PortSpec::new("signal", "digital-edge");
        signal.category = PortCategory::Signal;
        signal.min_connections = 2;
        signal.max_connections = Some(256);
        Ok(NodeDescription::new(
            vec![signal],
            [Capability::Connection, Capability::Dispatch],
        ))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::DigitalEdgeNet(options) = &node.options else {
            return Err(admission("net.digital-edge requires edge options"));
        };
        Ok(Box::new(DigitalEdgeNetwork {
            net: DigitalEdgeNet::new(options.clone())?,
        }))
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        descriptions: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for owner in &topology.nodes {
            if !matches!(owner.options, HostOptions::DigitalEdgeNet(_)) {
                continue;
            }
            let (mut sources, mut sinks) = (0, 0);
            for link in &topology.links {
                for (endpoint, peer) in [(&link.source, &link.target), (&link.target, &link.source)]
                {
                    if endpoint.node != owner.name {
                        continue;
                    }
                    let index = topology
                        .nodes
                        .iter()
                        .position(|node| node.name == peer.node)
                        .expect("core admitted endpoint");
                    let port = descriptions[index].port(&peer.port)?;
                    validate_port(&endpoint.port, port)?;
                    match port.direction {
                        Direction::Out => sources += 1,
                        Direction::In => sinks += 1,
                        Direction::InOut => unreachable!(),
                    }
                }
            }
            if sources != 1 || sinks == 0 {
                return Err(admission(
                    "digital-edge requires one source and at least one sink",
                ));
            }
        }
        Ok(())
    }
}
