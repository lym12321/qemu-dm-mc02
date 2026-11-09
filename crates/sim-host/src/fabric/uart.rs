//! Opaque UART chunks on one bounded point-to-point logical queue.
//!
//! Chunk boundaries are host reads, never messages. Endpoints and physical
//! transports are borrowed; neither queue delivery nor socket acceptance proves
//! firmware consumption. Callback replies use the supplied sole net owner.

use crate::runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry};
use sim_core::{
    Attachment, BuildContext, Capability, Component, ConnectionOwner, Direction, Dispatcher, Error,
    EventTime, Factory, Failure, NodeDescription, NodeSpec, PortCategory, PortSpec, Result,
    Session, TimeQuality, Topology,
};
use std::{
    any::Any,
    cell::{Cell, RefCell},
    cmp::Ordering,
    collections::BinaryHeap,
    rc::{Rc, Weak},
    sync::Arc,
};

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Debug)]
pub struct UartEndpointId(Rc<()>);
impl UartEndpointId {
    pub fn new() -> Self {
        Self(Rc::new(()))
    }
}
impl Default for UartEndpointId {
    fn default() -> Self {
        Self::new()
    }
}
impl PartialEq for UartEndpointId {
    fn eq(&self, other: &Self) -> bool {
        Rc::ptr_eq(&self.0, &other.0)
    }
}
impl Eq for UartEndpointId {}

#[derive(Clone, Debug)]
pub struct UartNetOptions {
    pub max_pending: usize,
    pub max_pending_bytes: usize,
    pub dispatch_budget: usize,
    pub delay_ns: u64,
}
impl Default for UartNetOptions {
    fn default() -> Self {
        Self {
            max_pending: 256,
            max_pending_bytes: 65536,
            dispatch_budget: 256,
            delay_ns: 0,
        }
    }
}
impl UartNetOptions {
    pub fn validate(&self) -> Result<()> {
        if !(1..=65536).contains(&self.max_pending)
            || !(1..=65536).contains(&self.dispatch_budget)
            || !(1..=4096 * 65536).contains(&self.max_pending_bytes)
        {
            return Err(admission(
                "UART chunk/dispatch capacities require 1..65536 and byte capacity 1..268435456",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug)]
pub struct UartStamp {
    pub source_time_ns: Option<u64>,
    pub mapped_time_ns: Option<u64>,
    pub quality: TimeQuality,
}
impl Default for UartStamp {
    fn default() -> Self {
        Self {
            source_time_ns: None,
            mapped_time_ns: None,
            quality: TimeQuality::Sampled,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UartObservation {
    pub source: String,
    pub destination: Option<String>,
    pub data: Vec<u8>,
    pub sequence: u64,
    pub due_ns: u64,
    pub source_time_ns: Option<u64>,
    pub mapped_time_ns: u64,
    pub delivery_time_ns: u64,
    pub quality: TimeQuality,
}

pub type UartReceiver = Rc<dyn Fn(&[u8], &mut UartNet) -> Result<()>>;
pub type UartObserver = Rc<dyn Fn(UartObservation) -> Result<()>>;
pub struct UartAttachment {
    pub endpoint: UartEndpointId,
    pub receive: UartReceiver,
}

#[derive(Clone)]
struct Peer {
    endpoint: UartEndpointId,
    name: String,
    receive: UartReceiver,
    last_mapped_ns: Option<u64>,
}
struct Pending {
    due_ns: u64,
    sequence: u64,
    source: usize,
    data: Vec<u8>,
    stamp: UartStamp,
}
impl PartialEq for Pending {
    fn eq(&self, other: &Self) -> bool {
        (self.due_ns, self.sequence) == (other.due_ns, other.sequence)
    }
}
impl Eq for Pending {}
impl PartialOrd for Pending {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Pending {
    fn cmp(&self, other: &Self) -> Ordering {
        (other.due_ns, other.sequence).cmp(&(self.due_ns, self.sequence))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct UartState {
    pub pending: usize,
    pub pending_bytes: usize,
    pub failed: bool,
}

/// Sole owner of logical chunks and terminal failure. Its observer and receiver
/// callbacks borrow capabilities; physical stream suffixes stay with transport.
pub struct UartNet {
    options: UartNetOptions,
    peers: Vec<Peer>,
    queue: BinaryHeap<Pending>,
    observe: UartObserver,
    pending_bytes: usize,
    sequence: u64,
    time_ns: u64,
    initialized: bool,
    closed: bool,
    dispatching: bool,
    failure: Option<Failure>,
}
impl UartNet {
    pub fn new(options: UartNetOptions, observe: UartObserver) -> Result<Self> {
        options.validate()?;
        Ok(Self {
            options,
            peers: vec![],
            queue: BinaryHeap::new(),
            observe,
            pending_bytes: 0,
            sequence: 0,
            time_ns: 0,
            initialized: false,
            closed: false,
            dispatching: false,
            failure: None,
        })
    }
    pub fn attach(&mut self, name: String, attachment: UartAttachment) -> Result<()> {
        if self.initialized || self.closed {
            return Err(state_error("UART endpoints must attach before reset"));
        }
        sim_core::Endpoint::parse(&name)?;
        if self
            .peers
            .iter()
            .any(|peer| peer.endpoint == attachment.endpoint || peer.name == name)
        {
            return Err(admission("UART endpoint is already attached"));
        }
        if self.peers.len() >= 2 {
            return Err(admission("UART net requires exactly two endpoints"));
        }
        self.peers.push(Peer {
            endpoint: attachment.endpoint,
            name,
            receive: attachment.receive,
            last_mapped_ns: None,
        });
        Ok(())
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        if self.closed || self.dispatching {
            return Err(state_error(
                "UART net cannot reset while closed or executing",
            ));
        }
        Session::new(session.session_id, session.t_sim_ns)?;
        if self.peers.len() != 2 {
            return Err(admission("UART net requires exactly two endpoints"));
        }
        self.queue.clear();
        for peer in &mut self.peers {
            peer.last_mapped_ns = None;
        }
        self.pending_bytes = 0;
        self.sequence = 0;
        self.time_ns = session.t_sim_ns;
        self.failure = None;
        self.initialized = true;
        Ok(())
    }
    fn healthy(&self) -> Result<()> {
        if let Some(failure) = &self.failure {
            return Err(failure.clone());
        }
        if self.closed || !self.initialized {
            return Err(state_error(
                "UART net requires a successful reset and healthy open state",
            ));
        }
        Ok(())
    }
    fn freeze(&mut self, error: Failure) -> Failure {
        self.failure.get_or_insert(error).clone()
    }
    pub fn failure(&self) -> Option<&Failure> {
        self.failure.as_ref()
    }
    pub fn observe(&self) -> UartState {
        UartState {
            pending: self.queue.len(),
            pending_bytes: self.pending_bytes,
            failed: self.failure.is_some(),
        }
    }
    pub fn dispatch_budget(&self) -> usize {
        self.options.dispatch_budget
    }
    pub fn next_due_ns(&self) -> Result<Option<u64>> {
        if let Some(failure) = &self.failure {
            return Err(failure.clone());
        }
        Ok(self.queue.peek().map(|pending| pending.due_ns))
    }
    fn publish(&mut self, observation: UartObservation) -> Result<()> {
        let observe = self.observe.clone();
        let result = observe(observation);
        self.healthy()?;
        result.map_err(|error| self.freeze(error))
    }
    pub fn send(
        &mut self,
        endpoint: &UartEndpointId,
        data: &[u8],
        mut stamp: UartStamp,
        cut: u64,
        not_before: u64,
    ) -> Result<()> {
        self.healthy()?;
        if data.len() > 4096 {
            return Err(admission("one UART chunk is limited to 4096 bytes"));
        }
        if data.is_empty() {
            return Ok(());
        }
        let source = self
            .peers
            .iter()
            .position(|peer| peer.endpoint == *endpoint)
            .ok_or_else(|| admission("UART source is not attached"))?;
        let mapped = stamp.mapped_time_ns.unwrap_or(cut);
        stamp.mapped_time_ns = Some(mapped);
        if self.peers[source]
            .last_mapped_ns
            .is_some_and(|previous| mapped < previous)
        {
            return Err(admission("UART source mapped time must not decrease"));
        }
        let due_ns = mapped
            .max(not_before)
            .checked_add(self.options.delay_ns)
            .ok_or_else(|| admission("UART delivery time exceeds u64"))?;
        let next_sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| admission("UART sequence exceeds u64"))?;
        if self.queue.len() >= self.options.max_pending
            || data.len()
                > self
                    .options
                    .max_pending_bytes
                    .saturating_sub(self.pending_bytes)
        {
            return Err(self.freeze(Arc::new(Error::Capacity(
                "UART pending chunk/byte capacity exhausted".into(),
            ))));
        }
        let data = data.to_vec();
        self.queue.push(Pending {
            due_ns,
            sequence: self.sequence,
            source,
            data: data.clone(),
            stamp,
        });
        self.pending_bytes += data.len();
        self.sequence = next_sequence;
        self.peers[source].last_mapped_ns = Some(mapped);
        self.publish(UartObservation {
            source: self.peers[source].name.clone(),
            destination: None,
            data,
            sequence: next_sequence - 1,
            due_ns,
            source_time_ns: stamp.source_time_ns,
            mapped_time_ns: mapped,
            delivery_time_ns: cut,
            quality: stamp.quality,
        })
    }
    pub fn dispatch(&mut self, cut: u64, budget: usize) -> Result<usize> {
        self.healthy()?;
        if self.dispatching {
            return Err(state_error("UART dispatch cannot be reentered"));
        }
        if cut < self.time_ns {
            return Err(admission("UART cut must not decrease"));
        }
        self.time_ns = cut;
        self.dispatching = true;
        let result = self.deliver(cut, budget.min(self.options.dispatch_budget));
        self.dispatching = false;
        result.map_err(|error| self.freeze(error))
    }
    fn deliver(&mut self, cut: u64, budget: usize) -> Result<usize> {
        let mut count = 0;
        while count < budget
            && self
                .queue
                .peek()
                .is_some_and(|pending| pending.due_ns <= cut)
        {
            let pending = self.queue.pop().expect("due head exists");
            self.pending_bytes -= pending.data.len();
            count += 1;
            let destination = 1 - pending.source;
            let receive = self.peers[destination].receive.clone();
            let result = receive(&pending.data, self);
            self.healthy()?;
            result?;
            self.publish(UartObservation {
                source: self.peers[pending.source].name.clone(),
                destination: Some(self.peers[destination].name.clone()),
                data: pending.data,
                sequence: pending.sequence,
                due_ns: pending.due_ns,
                source_time_ns: pending.stamp.source_time_ns,
                mapped_time_ns: pending
                    .stamp
                    .mapped_time_ns
                    .expect("send resolves mapped time"),
                delivery_time_ns: cut,
                quality: pending.stamp.quality,
            })?;
        }
        Ok(count)
    }
    pub fn close(&mut self) -> Result<()> {
        if self.dispatching {
            return Err(state_error("UART net cannot close during dispatch"));
        }
        self.closed = true;
        self.queue.clear();
        self.pending_bytes = 0;
        self.peers.clear();
        Ok(())
    }
}

fn send_from(
    context: &HostContext,
    endpoint: &UartEndpointId,
    net: &mut UartNet,
    data: &[u8],
    stamp: UartStamp,
) -> Result<()> {
    context.check_io()?;
    context.check_deadline()?;
    let result = net.send(
        endpoint,
        data,
        stamp,
        context.t_sim_ns()?,
        context.not_before_ns()?,
    );
    if let Some(primary) = net.failure() {
        return Err(context.fail(primary.clone()));
    }
    result
}

#[derive(Clone)]
pub struct UartConnection {
    net: Weak<RefCell<UartNet>>,
    endpoint: UartEndpointId,
    context: HostContext,
}
impl UartConnection {
    pub fn send(&self, data: &[u8], stamp: UartStamp) -> Result<()> {
        self.context.check_io()?;
        self.context.check_deadline()?;
        let owner = self
            .net
            .upgrade()
            .ok_or_else(|| state_error("UART net is no longer owned"))?;
        let mut net = owner.try_borrow_mut().map_err(|_| {
            self.context.fail(state_error(
                "UART receive callbacks must send through their supplied net",
            ))
        })?;
        send_from(&self.context, &self.endpoint, &mut net, data, stamp)
    }
    pub fn send_through(&self, net: &mut UartNet, data: &[u8], stamp: UartStamp) -> Result<()> {
        send_from(&self.context, &self.endpoint, net, data, stamp)
    }
}

pub struct UartNetwork {
    net: Rc<RefCell<UartNet>>,
}
impl UartNetwork {
    pub fn observe(&self) -> UartState {
        self.net.borrow().observe()
    }
}
impl ConnectionOwner<HostInput, HostEvent> for UartNetwork {
    fn attach_port(
        &mut self,
        attachment: Attachment<HostInput, HostEvent>,
    ) -> Result<Box<dyn Any>> {
        if attachment.owner_port != "uart"
            || attachment.descriptor.protocol != "uart"
            || attachment.descriptor.category != PortCategory::Protocol
            || attachment.descriptor.direction != Direction::InOut
        {
            return Err(admission("UART net requires inout UART protocol endpoints"));
        }
        let mut endpoint = *attachment
            .endpoint
            .downcast::<UartAttachment>()
            .map_err(|_| admission("UART connection requires a typed UART attachment"))?;
        let context = attachment.context;
        let receive = endpoint.receive;
        let receiver_context = context.clone();
        endpoint.receive = Rc::new(move |data, net| {
            receiver_context.check_io()?;
            receiver_context.check_deadline()?;
            let result = receive(data, net);
            if let Some(primary) = net.failure() {
                return Err(receiver_context.fail(primary.clone()));
            }
            if let Err(error) = result {
                return Err(receiver_context.fail(error));
            }
            receiver_context.check_io()?;
            receiver_context.check_deadline()?;
            Ok(())
        });
        let connection = UartConnection {
            net: Rc::downgrade(&self.net),
            endpoint: endpoint.endpoint.clone(),
            context,
        };
        self.net
            .borrow_mut()
            .attach(attachment.endpoint_name, endpoint)?;
        Ok(Box::new(connection))
    }
}
impl Dispatcher<HostInput, HostEvent> for UartNetwork {
    fn dispatch_budget(&self) -> usize {
        self.net.borrow().dispatch_budget()
    }
    fn next_due_ns(&self) -> Result<Option<u64>> {
        self.net.borrow().next_due_ns()
    }
    fn dispatch(&mut self, cut: u64, budget: usize, context: &HostContext) -> Result<usize> {
        context.check_io()?;
        self.net
            .borrow_mut()
            .dispatch(cut, budget)
            .map_err(|error| context.fail(error))
    }
}
impl Component<HostInput, HostEvent> for UartNetwork {
    fn close(&mut self) -> Result<()> {
        self.net.borrow_mut().close()
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        self.net.borrow_mut().reset(session)
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<HostInput, HostEvent>> {
        Some(self)
    }
    fn connection_owner(&mut self) -> Option<&mut dyn ConnectionOwner<HostInput, HostEvent>> {
        Some(self)
    }
}
fn publish(context: &HostContext, observation: UartObservation) -> Result<()> {
    context.check_io()?;
    let event = match observation.destination {
        Some(destination) => HostEvent::UartDelivery {
            source: observation.source,
            destination,
            data: observation.data,
            sequence: observation.sequence,
            due_ns: observation.due_ns,
        },
        None => HostEvent::UartSent {
            source: observation.source,
            data: observation.data,
            sequence: observation.sequence,
            due_ns: observation.due_ns,
        },
    };
    let kind = if matches!(event, HostEvent::UartDelivery { .. }) {
        "net.uart.delivery"
    } else {
        "net.uart.sent"
    };
    context.emit(
        Some("uart"),
        kind,
        event,
        EventTime {
            source_time_ns: observation.source_time_ns,
            mapped_time_ns: Some(observation.mapped_time_ns),
            delivery_time_ns: Some(observation.delivery_time_ns),
            quality: Some(observation.quality),
        },
    )?;
    context.check_io()?;
    context.check_deadline()
}

pub struct UartNetFactory;
impl Factory<HostOptions, HostInput, HostEvent> for UartNetFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::UartNet(options) = &node.options else {
            return Err(admission("net.uart requires UART options"));
        };
        options.validate()?;
        let mut port = PortSpec::new("uart", "uart");
        port.min_connections = 2;
        port.max_connections = Some(2);
        Ok(NodeDescription::new(
            vec![port],
            [Capability::Connection, Capability::Dispatch],
        ))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::UartNet(options) = &node.options else {
            return Err(admission("net.uart requires UART options"));
        };
        let context = build.context();
        let net = UartNet::new(
            options.clone(),
            Rc::new(move |observation| publish(&context, observation)),
        )?;
        Ok(Box::new(UartNetwork {
            net: Rc::new(RefCell::new(net)),
        }))
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        descriptions: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for owner in &topology.nodes {
            if !matches!(owner.options, HostOptions::UartNet(_)) {
                continue;
            }
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
                    if endpoint.port != "uart"
                        || port.protocol != "uart"
                        || port.category != PortCategory::Protocol
                        || port.direction != Direction::InOut
                    {
                        return Err(admission("UART net requires inout UART protocol endpoints"));
                    }
                }
            }
        }
        Ok(())
    }
}

pub struct UartEcho {
    closed: Rc<Cell<bool>>,
}
impl Component<HostInput, HostEvent> for UartEcho {
    fn close(&mut self) -> Result<()> {
        self.closed.set(true);
        Ok(())
    }
}
pub struct UartEchoFactory;
impl Factory<HostOptions, HostInput, HostEvent> for UartEchoFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        if !matches!(node.options, HostOptions::UartEcho) {
            return Err(admission("device.uart-echo has no options"));
        }
        let mut port = PortSpec::new("uart", "uart");
        port.min_connections = 1;
        Ok(NodeDescription::new(vec![port], []))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let endpoint = UartEndpointId::new();
        let source = endpoint.clone();
        let context = build.context();
        let closed = Rc::new(Cell::new(false));
        let receiver_closed = closed.clone();
        build.attach::<_, UartConnection>(
            "uart",
            UartAttachment {
                endpoint,
                receive: Rc::new(move |data, net| {
                    if receiver_closed.get() {
                        return Err(state_error("UART echo is closed"));
                    }
                    send_from(&context, &source, net, data, UartStamp::default())
                }),
            },
        )?;
        Ok(Box::new(UartEcho { closed }))
    }
}
