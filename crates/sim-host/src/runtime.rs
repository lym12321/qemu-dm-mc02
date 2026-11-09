//! Compiled host CAN factories using the protocol-neutral Simulation owner.
//!
//! The network owns the existing CAN bus; ports and receivers borrow it. Source
//! frame time remains distinct from the core delivery cut. This host fixture
//! does not advance a CPU or physics engine.

use crate::can::{CanBus, CanBusOptions, CanCallbacks, CanDelivery, CanFrame, EndpointId};
use sim_core::{
    Attachment, BuildContext, Capability, Component, ConnectionOwner, Context, Direction,
    Dispatcher, Endpoint, Error, EventTime, Factory, Failure, Input, InputValidator, LinkSpec,
    NodeDescription, NodeSpec, PortCategory, PortSpec, Registry, Result, Session, Simulation,
    SimulationOptions, Topology,
};
use std::{
    any::Any,
    cell::RefCell,
    collections::BTreeSet,
    rc::{Rc, Weak},
    sync::Arc,
};

#[derive(Clone, Debug)]
pub enum HostOptions {
    CanBus(CanNetworkOptions),
    CanHost,
    /// Worker transport ingress preserves RESET-mapped source timestamps.
    CanTransportBridge,
    UartNet(crate::fabric::uart::UartNetOptions),
    UartEcho,
    #[cfg(all(target_os = "linux", feature = "machine"))]
    Qemu(crate::machine::QemuOptions),
    #[cfg(feature = "mujoco")]
    Mujoco(crate::physics::factory::MujocoOptions),
    #[cfg(feature = "gazebo")]
    Gazebo(crate::physics::factory::GazeboOptions),
    #[cfg(feature = "devices")]
    M3508(crate::physics::factory::M3508Options),
    #[cfg(all(target_os = "linux", feature = "host-processes"))]
    HostProcesses(crate::processes::HostProcessesOptions),
    DigitalNet(crate::fabric::digital::DigitalNetOptions),
    DigitalEdgeNet(crate::fabric::edge::EdgeNetOptions),
    #[cfg(all(target_os = "linux", feature = "socketcan"))]
    SocketCan(crate::endpoints::socketcan::SocketCanOptions),
    ImuNet(crate::fabric::imu::ImuNetOptions),
    #[cfg(feature = "referee")]
    Referee(crate::devices::referee::factory::RefereeOptions),
    /// Resource-free observer configured only by the experiment app.
    ObservationCut,
}

#[derive(Clone, Debug, PartialEq)]
pub enum HostInput {
    #[cfg(feature = "referee")]
    RefereeEnable(bool),
    CanFrame(CanFrame),
    FaultProfile(String),
    #[cfg(all(target_os = "linux", feature = "machine"))]
    Machine(crate::profile::MachineInput),
    #[cfg(feature = "physics")]
    JointEffort(Vec<(String, f64)>),
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CanDropRule {
    pub source: Option<Endpoint>,
    pub destination: Option<Endpoint>,
    pub can_id: Option<u32>,
}

#[derive(Clone, Debug)]
pub struct CanFaultProfile {
    pub name: String,
    pub delay_ns: Option<u64>,
    pub drop: Vec<CanDropRule>,
}

#[derive(Clone, Debug, Default)]
pub struct CanNetworkOptions {
    pub bus: CanBusOptions,
    pub fault_profiles: Vec<CanFaultProfile>,
}

impl From<CanBusOptions> for CanNetworkOptions {
    fn from(bus: CanBusOptions) -> Self {
        Self {
            bus,
            fault_profiles: vec![],
        }
    }
}

struct Policy {
    name: String,
    delay_ns: u64,
    drop: Vec<CanDropRule>,
}

impl CanNetworkOptions {
    fn policies(&self) -> Result<Vec<Policy>> {
        if !(1..=65536).contains(&self.bus.max_pending)
            || !(1..=65536).contains(&self.bus.dispatch_budget)
            || self.fault_profiles.len() > 16
        {
            return Err(admission(
                "CAN capacities require 1..65536 and at most 16 fault profiles",
            ));
        }
        let mut names = BTreeSet::from(["normal"]);
        let mut rules = 0;
        let mut policies = vec![Policy {
            name: "normal".into(),
            delay_ns: self.bus.delay_ns,
            drop: vec![],
        }];
        for profile in &self.fault_profiles {
            if profile.name.is_empty()
                || profile.name.len() > 64
                || !profile.name.bytes().enumerate().all(|(i, b)| {
                    b.is_ascii_alphabetic()
                        || b == b'_'
                        || i > 0 && (b.is_ascii_digit() || b == b'-')
                })
                || !names.insert(&profile.name)
            {
                return Err(admission(
                    "CAN fault profile names must be unique ASCII identifiers up to 64 bytes; normal is reserved",
                ));
            }
            rules += profile.drop.len();
            if rules > 64 {
                return Err(admission(
                    "CAN fault profiles permit at most 64 drop rules total",
                ));
            }
            for rule in &profile.drop {
                if rule.source.is_none() && rule.destination.is_none() && rule.can_id.is_none()
                    || rule.can_id.is_some_and(|id| id > 0x1fff_ffff)
                {
                    return Err(admission(
                        "CAN drop requires source, destination or a 29-bit CAN ID",
                    ));
                }
                for endpoint in [&rule.source, &rule.destination].into_iter().flatten() {
                    Endpoint::new(&endpoint.node, &endpoint.port)?;
                }
            }
            policies.push(Policy {
                name: profile.name.clone(),
                delay_ns: profile.delay_ns.unwrap_or(self.bus.delay_ns),
                drop: profile.drop.clone(),
            });
        }
        Ok(policies)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum HostEvent {
    #[cfg(feature = "referee")]
    RefereeBatch {
        data: Vec<u8>,
        first_sequence: u8,
        next_sequence: u8,
        publication: u64,
        period_ns: u64,
        due_ns: u64,
        next_due_ns: u64,
    },
    ImuSample {
        sample: crate::signals::ImuSignalSample,
    },
    ImuDelivery {
        source: String,
        destination: String,
        sample: crate::signals::ImuSignalSample,
        sequence: u64,
        due_ns: u64,
    },
    #[cfg(all(target_os = "linux", feature = "host-processes"))]
    ProcessStarted {
        name: String,
        pid: u32,
    },
    #[cfg(all(target_os = "linux", feature = "host-processes"))]
    ProcessOutput {
        name: String,
        stdout: String,
        stderr: String,
    },
    #[cfg(feature = "physics")]
    PhysicsState(crate::physics::factory::PhysicsState),
    #[cfg(feature = "devices")]
    M3508State {
        current_raw: i16,
        rejected_commands: u64,
    },
    UartSent {
        source: String,
        data: Vec<u8>,
        sequence: u64,
        due_ns: u64,
    },
    UartDelivery {
        source: String,
        destination: String,
        data: Vec<u8>,
        sequence: u64,
        due_ns: u64,
    },
    #[cfg(all(target_os = "linux", feature = "machine"))]
    Snapshot {
        value: serde_json::Value,
    },
    #[cfg(all(target_os = "linux", feature = "machine"))]
    MachineUart {
        data: Vec<u8>,
    },
    #[cfg(all(target_os = "linux", feature = "machine"))]
    MachineCanTx {
        frame: CanFrame,
    },
    #[cfg(all(target_os = "linux", feature = "machine"))]
    CanInputProcessed {
        value: serde_json::Value,
    },
    #[cfg(all(target_os = "linux", feature = "machine"))]
    Cosim {
        value: serde_json::Value,
    },
    CanSent {
        frame: CanFrame,
    },
    CanDelivery {
        destination: String,
        frame: CanFrame,
        sequence: u64,
        due_ns: u64,
        late: bool,
        dropped: bool,
    },
    CanHostReceived {
        frame: CanFrame,
    },
    CanFaultApplied {
        previous_profile: String,
        profile: String,
        delay_ns: u64,
        drop: Vec<CanDropRule>,
        cut_ns: u64,
    },
    DigitalSample {
        source: String,
        level: Option<bool>,
        active: bool,
        changed: bool,
    },
    DigitalDelivery {
        source: String,
        destination: String,
        level: bool,
        sequence: u64,
        due_ns: u64,
    },
    DigitalEdgeDelivery {
        source: String,
        destination: String,
        level: bool,
        sequence: u64,
        due_ns: u64,
    },
}

pub type HostSimulation = Simulation<HostOptions, HostInput, HostEvent>;
pub type HostContext = Context<HostInput, HostEvent>;
pub type HostRegistry = Registry<HostOptions, HostInput, HostEvent>;

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}
fn lower(error: crate::Failure) -> Failure {
    error
}

/// A receiver may respond through the supplied sole bus while delivery is in
/// progress. It must not reenter the network via a borrowed connection.
pub type CanReceiver = Rc<dyn Fn(&CanFrame, &mut CanBus) -> Result<()>>;

pub struct CanAttachment {
    pub endpoint: EndpointId,
    pub receive: CanReceiver,
}

struct Route {
    endpoint: EndpointId,
    context: HostContext,
    port: String,
    receive: CanReceiver,
}
struct NetworkState {
    bus: CanBus,
    routes: Vec<Route>,
    policies: Vec<Policy>,
    profile: usize,
}

/// Borrowed send capability. The network is the only strong owner and close
/// clears its queue. Every send applies the active Simulation dispatch floor.
pub struct CanConnection {
    network: Weak<RefCell<NetworkState>>,
    endpoint: EndpointId,
    context: HostContext,
    port: String,
}
impl CanConnection {
    pub fn send(&mut self, frame: CanFrame) -> Result<()> {
        self.context.check_io()?;
        self.context.check_deadline()?;
        let network = self
            .network
            .upgrade()
            .ok_or_else(|| state_error("CAN network is no longer owned"))?;
        {
            let mut network = network.try_borrow_mut().map_err(|_| {
                state_error("CAN receive callbacks must send through their supplied bus")
            })?;
            if let Err(error) = network.bus.send(
                &self.endpoint,
                frame.clone(),
                Some(self.context.not_before_ns()?),
            ) {
                if let Some(primary) = network.bus.failure() {
                    return Err(self.context.fail(lower(primary.clone())));
                }
                return Err(lower(error));
            }
        }
        self.context.emit(
            Some(&self.port),
            "can.sent",
            HostEvent::CanSent {
                frame: frame.clone(),
            },
            EventTime {
                source_time_ns: Some(frame.timestamp_ns()),
                mapped_time_ns: Some(frame.timestamp_ns()),
                ..Default::default()
            },
        )?;
        Ok(())
    }
}

pub struct CanNetwork {
    network: Rc<RefCell<NetworkState>>,
    context: HostContext,
}
impl CanNetwork {
    pub fn pending(&self) -> usize {
        self.network.borrow().bus.pending()
    }
    pub fn profile(&self) -> String {
        let network = self.network.borrow();
        network.policies[network.profile].name.clone()
    }

    pub fn apply_profile(&mut self, name: &str) -> Result<()> {
        self.context.check_io()?;
        self.context.check_deadline()?;
        let event = {
            let mut network = self.network.try_borrow_mut().map_err(|_| {
                self.context
                    .fail(state_error("CAN policy cannot change during delivery"))
            })?;
            if network.policies.len() == 1 {
                return Err(admission("CAN fault requires configured profiles"));
            }
            let profile = network
                .policies
                .iter()
                .position(|profile| profile.name == name)
                .ok_or_else(|| {
                    admission("CAN fault input requires a configured profile name or normal")
                })?;
            let previous_profile = network.policies[network.profile].name.clone();
            let delay_ns = network.policies[profile].delay_ns;
            network
                .bus
                .set_delay_ns(delay_ns)
                .map_err(|error| self.context.fail(lower(error)))?;
            network.profile = profile;
            HostEvent::CanFaultApplied {
                previous_profile,
                profile: name.to_owned(),
                delay_ns,
                drop: network.policies[profile].drop.clone(),
                cut_ns: self.context.t_sim_ns()?,
            }
        };
        let cut = self.context.t_sim_ns()?;
        self.context.emit(
            Some("fault"),
            "can.fault.applied",
            event,
            EventTime {
                source_time_ns: Some(cut),
                mapped_time_ns: Some(cut),
                delivery_time_ns: Some(cut),
                ..Default::default()
            },
        )?;
        Ok(())
    }
}
struct Routes<'a> {
    routes: &'a [Route],
    drop: &'a [CanDropRule],
}
impl Routes<'_> {
    fn route(&self, endpoint: &EndpointId) -> crate::Result<&Route> {
        self.routes
            .iter()
            .find(|route| route.endpoint == *endpoint)
            .ok_or_else(|| Arc::new(crate::Error::State("CAN receiver is not attached")))
    }
    fn callback_error(context: &HostContext, error: Failure) -> crate::Failure {
        let primary = context.fail(error);
        Arc::new(crate::Error::Callback(primary.to_string()))
    }
}
impl CanCallbacks for Routes<'_> {
    fn should_drop(&mut self, delivery: &CanDelivery<'_>, _: &mut CanBus) -> crate::Result<bool> {
        let source = self.route(delivery.source)?;
        source
            .context
            .check_io()
            .map_err(|error| Self::callback_error(&source.context, error))?;
        Ok(self.drop.iter().any(|rule| {
            rule.source
                .as_ref()
                .is_none_or(|endpoint| endpoint.to_string() == delivery.source_name)
                && rule
                    .destination
                    .as_ref()
                    .is_none_or(|endpoint| endpoint.to_string() == delivery.destination_name)
                && rule.can_id.is_none_or(|id| id == delivery.frame.can_id())
        }))
    }
    fn observe(&mut self, delivery: &CanDelivery<'_>, _: &mut CanBus) -> crate::Result<()> {
        let source = self.route(delivery.source)?;
        source
            .context
            .emit(
                Some(&source.port),
                "can.delivery",
                HostEvent::CanDelivery {
                    destination: delivery.destination_name.to_owned(),
                    frame: delivery.frame.clone(),
                    sequence: delivery.sequence,
                    due_ns: delivery.due_ns,
                    late: delivery.late,
                    dropped: delivery.dropped,
                },
                EventTime {
                    source_time_ns: Some(delivery.frame.timestamp_ns()),
                    mapped_time_ns: Some(delivery.frame.timestamp_ns()),
                    delivery_time_ns: Some(delivery.delivered_ns),
                    ..Default::default()
                },
            )
            .map_err(|error| Self::callback_error(&source.context, error))?;
        Ok(())
    }
    fn receive(
        &mut self,
        destination: &EndpointId,
        frame: &CanFrame,
        bus: &mut CanBus,
    ) -> crate::Result<()> {
        let destination = self.route(destination)?;
        destination
            .context
            .check_io()
            .map_err(|error| Self::callback_error(&destination.context, error))?;
        (destination.receive)(frame, bus)
            .map_err(|error| Self::callback_error(&destination.context, error))?;
        destination
            .context
            .check_deadline()
            .map_err(|error| Self::callback_error(&destination.context, error))
    }
}
impl ConnectionOwner<HostInput, HostEvent> for CanNetwork {
    fn attach_port(
        &mut self,
        attachment: Attachment<HostInput, HostEvent>,
    ) -> Result<Box<dyn Any>> {
        if attachment.owner_port != "can"
            || attachment.descriptor.protocol != "can"
            || attachment.descriptor.direction != Direction::InOut
            || attachment.descriptor.category != PortCategory::Protocol
        {
            return Err(admission(
                "CAN connections require inout CAN protocol ports",
            ));
        }
        let endpoint = attachment
            .endpoint
            .downcast::<CanAttachment>()
            .map_err(|_| admission("CAN connection requires a typed CAN attachment"))?;
        let mut network = self.network.borrow_mut();
        network
            .bus
            .attach(&endpoint.endpoint, attachment.endpoint_name)
            .map_err(lower)?;
        let connection = CanConnection {
            network: Rc::downgrade(&self.network),
            endpoint: endpoint.endpoint.clone(),
            context: attachment.context.clone(),
            port: attachment.descriptor.name.clone(),
        };
        network.routes.push(Route {
            endpoint: endpoint.endpoint,
            context: attachment.context,
            port: attachment.descriptor.name,
            receive: endpoint.receive,
        });
        Ok(Box::new(connection))
    }
}
impl Dispatcher<HostInput, HostEvent> for CanNetwork {
    fn dispatch_budget(&self) -> usize {
        self.network.borrow().bus.dispatch_budget()
    }
    fn next_due_ns(&self) -> Result<Option<u64>> {
        self.network.borrow().bus.next_due_ns().map_err(lower)
    }
    fn dispatch(&mut self, cut: u64, budget: usize, context: &HostContext) -> Result<usize> {
        context.check_io()?;
        let mut network = self.network.borrow_mut();
        let NetworkState {
            bus,
            routes,
            policies,
            profile,
        } = &mut *network;
        bus.step(
            cut,
            Some(budget),
            &mut Routes {
                routes,
                drop: &policies[*profile].drop,
            },
        )
        .map_err(|error| context.fail(lower(error)))
    }
}
impl Component<HostInput, HostEvent> for CanNetwork {
    fn close(&mut self) -> Result<()> {
        self.network.borrow_mut().bus.close().map_err(lower)
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        let mut network = self.network.borrow_mut();
        network.bus.reset(session.t_sim_ns).map_err(lower)?;
        let delay_ns = network.policies[0].delay_ns;
        network.bus.set_delay_ns(delay_ns).map_err(lower)?;
        network.profile = 0;
        Ok(())
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<HostInput, HostEvent>> {
        Some(self)
    }
    fn connection_owner(&mut self) -> Option<&mut dyn ConnectionOwner<HostInput, HostEvent>> {
        Some(self)
    }
    fn input(&mut self) -> Option<&mut dyn Input<HostInput, HostEvent>> {
        Some(self)
    }
}

impl Input<HostInput, HostEvent> for CanNetwork {
    fn input(&mut self, port: &str, value: HostInput, _: &HostContext) -> Result<()> {
        let HostInput::FaultProfile(profile) = value else {
            return Err(admission("CAN fault input requires a profile name"));
        };
        if port != "fault" {
            return Err(admission("CAN fault input requires fault port"));
        }
        self.apply_profile(&profile)
    }
}

pub struct CanHost {
    connection: CanConnection,
    closed: bool,
    preserve_timestamp: bool,
}
impl CanHost {
    /// Host stimulus uses the current input cut, rather than caller timestamps.
    pub fn send(&mut self, frame: CanFrame) -> Result<()> {
        if self.closed {
            return Err(state_error("CAN host is closed"));
        }
        if self.preserve_timestamp {
            self.connection.send(frame)
        } else {
            self.connection
                .send(frame.with_timestamp(self.connection.context.t_sim_ns()?))
        }
    }
}
impl Input<HostInput, HostEvent> for CanHost {
    fn input(&mut self, port: &str, value: HostInput, _: &HostContext) -> Result<()> {
        if port != "tx" {
            return Err(admission("CAN host input requires tx"));
        }
        let HostInput::CanFrame(frame) = value else {
            return Err(admission("CAN host input requires a CAN frame"));
        };
        self.send(frame)
    }
}
impl Component<HostInput, HostEvent> for CanHost {
    fn close(&mut self) -> Result<()> {
        self.closed = true;
        Ok(())
    }
    fn input(&mut self) -> Option<&mut dyn Input<HostInput, HostEvent>> {
        Some(self)
    }
}

pub struct CanBusFactory;
impl InputValidator<HostOptions, HostInput> for CanBusFactory {
    fn validate_input(
        &self,
        node: &NodeSpec<HostOptions>,
        port: &str,
        value: HostInput,
        connected_ports: &[String],
    ) -> Result<HostInput> {
        let HostOptions::CanBus(options) = &node.options else {
            return Err(admission("bus.can requires CAN network options"));
        };
        let HostInput::FaultProfile(profile) = &value else {
            return Err(admission("CAN fault input requires a profile name"));
        };
        if port != "fault"
            || connected_ports.iter().any(|port| port == "fault")
            || options.fault_profiles.is_empty()
            || profile != "normal"
                && !options
                    .fault_profiles
                    .iter()
                    .any(|option| option.name == *profile)
        {
            return Err(admission(
                "CAN fault accepts external configured profile names or normal only",
            ));
        }
        Ok(value)
    }
}
impl Factory<HostOptions, HostInput, HostEvent> for CanBusFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::CanBus(options) = &node.options else {
            return Err(admission("bus.can requires CAN bus options"));
        };
        let policies = options.policies()?;
        let mut can = PortSpec::new("can", "can");
        can.max_connections = None;
        let mut ports = vec![can];
        let mut capabilities = vec![Capability::Connection, Capability::Dispatch];
        if policies.len() > 1 {
            let mut fault = PortSpec::new("fault", "can-fault");
            fault.direction = Direction::In;
            fault.category = PortCategory::Signal;
            fault.capabilities.insert("external_input".into());
            ports.push(fault);
            capabilities.push(Capability::Input);
        }
        Ok(NodeDescription::new(ports, capabilities))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::CanBus(options) = &node.options else {
            return Err(admission("bus.can requires CAN bus options"));
        };
        Ok(Box::new(CanNetwork {
            network: Rc::new(RefCell::new(NetworkState {
                bus: CanBus::new(options.bus.clone()).map_err(lower)?,
                routes: vec![],
                policies: options.policies()?,
                profile: 0,
            })),
            context: build.context(),
        }))
    }
    fn input_validator(&self) -> Option<&dyn InputValidator<HostOptions, HostInput>> {
        Some(self)
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        descriptions: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for owner in &topology.nodes {
            let HostOptions::CanBus(options) = &owner.options else {
                continue;
            };
            let mut members = BTreeSet::new();
            for link in &topology.links {
                for (endpoint, peer) in [(&link.source, &link.target), (&link.target, &link.source)]
                {
                    if endpoint.node != owner.name {
                        continue;
                    }
                    if endpoint.port != "can" {
                        return Err(admission(
                            "CAN fault accepts external input only; connect its can port",
                        ));
                    }
                    for endpoint in [endpoint, peer] {
                        let index = topology
                            .nodes
                            .iter()
                            .position(|node| node.name == endpoint.node)
                            .expect("core admitted endpoint");
                        let port = descriptions[index].port(&endpoint.port)?;
                        if port.category != PortCategory::Protocol
                            || port.protocol != "can"
                            || port.direction != Direction::InOut
                        {
                            return Err(admission(
                                "CAN connections require inout CAN protocol ports",
                            ));
                        }
                    }
                    members.insert(peer.clone());
                }
            }
            for profile in &options.fault_profiles {
                for rule in &profile.drop {
                    for endpoint in [&rule.source, &rule.destination].into_iter().flatten() {
                        if !members.contains(endpoint) {
                            return Err(admission("CAN drop endpoint is not attached to this bus"));
                        }
                    }
                }
            }
        }
        Ok(())
    }
}

pub struct CanHostFactory;
impl InputValidator<HostOptions, HostInput> for CanHostFactory {
    fn validate_input(
        &self,
        node: &NodeSpec<HostOptions>,
        port: &str,
        value: HostInput,
        _: &[String],
    ) -> Result<HostInput> {
        if !matches!(
            node.options,
            HostOptions::CanHost | HostOptions::CanTransportBridge
        ) || port != "tx"
        {
            return Err(admission("CAN host input requires tx"));
        }
        if !matches!(value, HostInput::CanFrame(_)) {
            return Err(admission("CAN host input requires a CAN frame"));
        }
        // CanFrame fields are private and already admitted by its constructor.
        Ok(value)
    }
}
impl Factory<HostOptions, HostInput, HostEvent> for CanHostFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        if !matches!(
            node.options,
            HostOptions::CanHost | HostOptions::CanTransportBridge
        ) {
            return Err(admission("device.can-host has no options"));
        }
        let mut can = PortSpec::new("can", "can");
        can.min_connections = 1;
        let mut tx = PortSpec::new("tx", "can-frame");
        tx.direction = Direction::In;
        tx.category = PortCategory::Signal;
        tx.capabilities.insert("external_input".into());
        Ok(NodeDescription::new(vec![can, tx], [Capability::Input]))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let context = build.context();
        let receiver: CanReceiver = Rc::new(move |frame, _| {
            context.emit(
                Some("can"),
                "can-host.received",
                HostEvent::CanHostReceived {
                    frame: frame.clone(),
                },
                EventTime {
                    source_time_ns: Some(frame.timestamp_ns()),
                    mapped_time_ns: Some(frame.timestamp_ns()),
                    ..Default::default()
                },
            )?;
            Ok(())
        });
        let connection = build.attach(
            "can",
            CanAttachment {
                endpoint: EndpointId::new(),
                receive: receiver,
            },
        )?;
        Ok(Box::new(CanHost {
            connection,
            closed: false,
            preserve_timestamp: matches!(node.options, HostOptions::CanTransportBridge),
        }))
    }
    fn input_validator(&self) -> Option<&dyn InputValidator<HostOptions, HostInput>> {
        Some(self)
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        _: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for link in &topology.links {
            for endpoint in [&link.source, &link.target] {
                if endpoint.port == "tx"
                    && topology.nodes.iter().any(|node| {
                        node.name == endpoint.node
                            && matches!(
                                node.options,
                                HostOptions::CanHost | HostOptions::CanTransportBridge
                            )
                    })
                {
                    return Err(admission(
                        "CAN host tx accepts external input only; connect its can port",
                    ));
                }
            }
        }
        Ok(())
    }
}

pub fn registry() -> Result<HostRegistry> {
    let mut registry = Registry::default();
    registry.register("bus.can", CanBusFactory)?;
    let host_can = registry.register("device.can-host", CanHostFactory)?;
    registry.alias("host.can", host_can)?;
    #[cfg(all(target_os = "linux", feature = "socketcan"))]
    registry.register(
        "host.socketcan",
        crate::endpoints::socketcan::SocketCanFactory,
    )?;
    registry.register("net.digital", crate::fabric::digital::DigitalNetFactory)?;
    registry.register(
        "net.digital-edge",
        crate::fabric::edge::DigitalEdgeNetFactory,
    )?;
    registry.register("net.imu", crate::fabric::imu::ImuNetFactory)?;
    #[cfg(feature = "referee")]
    registry.register(
        "device.referee",
        crate::devices::referee::factory::RefereeFactory,
    )?;
    registry.register("net.uart", crate::fabric::uart::UartNetFactory)?;
    registry.register("device.uart-echo", crate::fabric::uart::UartEchoFactory)?;
    #[cfg(all(target_os = "linux", feature = "machine"))]
    registry.register("machine.qemu", crate::machine::QemuFactory)?;
    #[cfg(feature = "mujoco")]
    registry.register("physics.mujoco", crate::physics::factory::NativeFactory)?;
    #[cfg(feature = "gazebo")]
    registry.register("physics.gazebo", crate::physics::factory::NativeFactory)?;
    #[cfg(feature = "devices")]
    registry.register("device.dji-m3508", crate::physics::factory::M3508Factory)?;
    #[cfg(all(target_os = "linux", feature = "host-processes"))]
    registry.register("app.host-processes", crate::processes::HostProcessesFactory)?;
    Ok(registry)
}

/// A real host-only consumer useful without QEMU or an external plant. The
/// caller may instead build the same admitted nodes and links programmatically.
pub fn host_can(
    options: impl Into<CanNetworkOptions>,
    simulation: SimulationOptions,
) -> Result<HostSimulation> {
    let nodes = vec![
        NodeSpec::new("bus", "bus.can", HostOptions::CanBus(options.into())),
        NodeSpec::new("left", "device.can-host", HostOptions::CanHost),
        NodeSpec::new("right", "device.can-host", HostOptions::CanHost),
    ];
    let links = [("left.can", "bus.can"), ("right.can", "bus.can")]
        .into_iter()
        .map(|(source, target)| {
            Ok(LinkSpec::new(
                sim_core::Endpoint::parse(source)?,
                sim_core::Endpoint::parse(target)?,
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    HostSimulation::new(Topology::new(nodes, links)?, registry()?, simulation)
}
