//! Sampled logical fanout, without an electrical solver or an edge clock.
//!
//! One driver is sampled once per completed cut. Changed levels are delivered
//! to sinks in attachment order, after every source has been sampled. Missing
//! initial drive leaves inputs untouched; losing active drive freezes the net.

pub use super::sampled::NetOptions as DigitalNetOptions;
use super::sampled::{self, Fanout, Policy};
use crate::runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry};
use sim_core::{
    Attachment, BuildContext, Capability, Component, ConnectionOwner, Direction, Dispatcher, Error,
    EventTime, Factory, Failure, NodeDescription, NodeSpec, PortCategory, PortSpec, Result,
    Sampler, Session, TimeQuality, Topology,
};
use std::{any::Any, rc::Rc, sync::Arc};

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DigitalSample {
    pub level: Option<bool>,
    pub source_time_ns: u64,
    pub mapped_time_ns: u64,
}

pub type DigitalSource = Rc<dyn Fn() -> Result<DigitalSample>>;
pub type DigitalSink = Rc<dyn Fn(bool) -> Result<()>>;

/// Callbacks borrow the endpoint's completed state and input capability. The
/// net never resets or closes the endpoint represented by either callback.
pub enum DigitalAttachment {
    Source(DigitalSource),
    Sink(DigitalSink),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DigitalObservation {
    Sample {
        source: String,
        sample: DigitalSample,
        changed: bool,
        cut_ns: u64,
    },
    Delivery {
        source: String,
        destination: String,
        sample: DigitalSample,
        sequence: u64,
        due_ns: u64,
        cut_ns: u64,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DigitalState {
    pub level: Option<bool>,
    pub sample_time_ns: Option<u64>,
    pub pending: usize,
    pub failed: bool,
}

#[derive(Default)]
struct DigitalPolicy;
impl Policy for DigitalPolicy {
    const NAME: &'static str = "digital";
    type Sample = DigitalSample;
    type Delivery = bool;
    fn validate(
        &self,
        sample: DigitalSample,
        previous: Option<DigitalSample>,
        cut: u64,
    ) -> Result<bool> {
        if sample.mapped_time_ns > cut {
            return Err(admission("digital sample is beyond the completed cut"));
        }
        if previous.is_some_and(|previous| {
            sample.mapped_time_ns < previous.mapped_time_ns
                || sample.source_time_ns < previous.source_time_ns
        }) {
            return Err(admission("digital sample time must not decrease"));
        }
        let previous_level = previous.and_then(|previous| previous.level);
        if previous_level.is_some() && sample.level.is_none() {
            return Err(state_error(
                "digital source lost output drive after activation; release is unsupported",
            ));
        }
        Ok(sample.level.is_some() && sample.level != previous_level)
    }
    fn delivery(sample: DigitalSample) -> bool {
        sample.level.expect("only driven levels enqueue")
    }
}

impl From<sampled::Observation<DigitalSample>> for DigitalObservation {
    fn from(value: sampled::Observation<DigitalSample>) -> Self {
        match value {
            sampled::Observation::Sample {
                source,
                sample,
                enqueued,
                cut_ns,
            } => Self::Sample {
                source,
                sample,
                changed: enqueued,
                cut_ns,
            },
            sampled::Observation::Delivery {
                source,
                destination,
                sample,
                sequence,
                due_ns,
                cut_ns,
            } => Self::Delivery {
                source,
                destination,
                sample,
                sequence,
                due_ns,
                cut_ns,
            },
        }
    }
}

/// Borrows endpoints and applies logical drive policy to the shared fanout owner.
pub struct DigitalNet {
    fanout: Fanout<DigitalPolicy>,
}
impl DigitalNet {
    pub fn new(options: DigitalNetOptions) -> Result<Self> {
        Ok(Self {
            fanout: Fanout::new(options)?,
        })
    }
    pub fn attach(&mut self, name: String, endpoint: DigitalAttachment) -> Result<()> {
        self.fanout.attach(
            name,
            match endpoint {
                DigitalAttachment::Source(read) => sampled::Attachment::Source(read),
                DigitalAttachment::Sink(receive) => sampled::Attachment::Sink(receive),
            },
        )
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        self.fanout.reset(session)
    }
    pub fn failure(&self) -> Option<&Failure> {
        self.fanout.failure()
    }
    pub fn observe(&self) -> DigitalState {
        let state = self.fanout.observe();
        DigitalState {
            level: state.sample.and_then(|sample| sample.level),
            sample_time_ns: state.sample_time_ns,
            pending: state.pending,
            failed: state.failed,
        }
    }
    pub fn dispatch_budget(&self) -> usize {
        self.fanout.dispatch_budget()
    }
    pub fn next_due_ns(&self) -> Option<u64> {
        self.fanout.next_due_ns()
    }
    pub fn sample(
        &mut self,
        cut: u64,
        observe: &mut impl FnMut(DigitalObservation) -> Result<()>,
    ) -> Result<()> {
        self.fanout.sample(cut, &mut |event| observe(event.into()))
    }
    pub fn dispatch(
        &mut self,
        cut: u64,
        budget: usize,
        observe: &mut impl FnMut(DigitalObservation) -> Result<()>,
    ) -> Result<usize> {
        self.fanout
            .dispatch(cut, budget, &mut |event| observe(event.into()))
    }
    pub fn close(&mut self) {
        self.fanout.close();
    }
}

fn emit(context: &HostContext, observation: DigitalObservation) -> Result<()> {
    let (event, sample, cut, kind) = match observation {
        DigitalObservation::Sample {
            source,
            sample,
            changed,
            cut_ns,
        } => (
            HostEvent::DigitalSample {
                source,
                level: sample.level,
                active: sample.level.is_some(),
                changed,
            },
            sample,
            cut_ns,
            "net.digital.sample",
        ),
        DigitalObservation::Delivery {
            source,
            destination,
            sample,
            sequence,
            due_ns,
            cut_ns,
        } => (
            HostEvent::DigitalDelivery {
                source,
                destination,
                level: sample.level.expect("driven delivery"),
                sequence,
                due_ns,
            },
            sample,
            cut_ns,
            "net.digital.delivery",
        ),
    };
    context.emit(
        Some("signal"),
        kind,
        event,
        EventTime {
            source_time_ns: Some(sample.source_time_ns),
            mapped_time_ns: Some(sample.mapped_time_ns),
            delivery_time_ns: Some(cut),
            quality: Some(TimeQuality::Sampled),
        },
    )?;
    context.check_io()?;
    context.check_deadline()
}

pub struct DigitalNetwork {
    net: DigitalNet,
}
impl DigitalNetwork {
    pub fn observe(&self) -> DigitalState {
        self.net.observe()
    }
}
impl ConnectionOwner<HostInput, HostEvent> for DigitalNetwork {
    fn attach_port(
        &mut self,
        attachment: Attachment<HostInput, HostEvent>,
    ) -> Result<Box<dyn Any>> {
        let descriptor = &attachment.descriptor;
        if attachment.owner_port != "signal"
            || descriptor.protocol != "digital"
            || descriptor.category != PortCategory::Signal
            || !matches!(descriptor.direction, Direction::In | Direction::Out)
        {
            return Err(admission(
                "digital endpoint must be an input or output digital signal",
            ));
        }
        let endpoint = *attachment
            .endpoint
            .downcast::<DigitalAttachment>()
            .map_err(|_| admission("digital connection requires a typed digital attachment"))?;
        let context = attachment.context;
        let endpoint = match (descriptor.direction, endpoint) {
            (Direction::Out, DigitalAttachment::Source(read)) => {
                DigitalAttachment::Source(Rc::new(move || {
                    context.check_io()?;
                    context.check_deadline()?;
                    let sample = read()?;
                    context.check_io()?;
                    context.check_deadline()?;
                    Ok(sample)
                }))
            }
            (Direction::In, DigitalAttachment::Sink(receive)) => {
                DigitalAttachment::Sink(Rc::new(move |level| {
                    context.check_io()?;
                    context.check_deadline()?;
                    receive(level)?;
                    context.check_io()?;
                    context.check_deadline()
                }))
            }
            _ => {
                return Err(admission(
                    "digital attachment role must match port direction",
                ));
            }
        };
        self.net.attach(attachment.endpoint_name, endpoint)?;
        Ok(Box::new(()))
    }
}
impl Sampler<HostInput, HostEvent> for DigitalNetwork {
    fn sample(&mut self, cut: u64, context: &HostContext) -> Result<()> {
        context.check_io()?;
        self.net
            .sample(cut, &mut |event| emit(context, event))
            .map_err(|error| context.fail(error))
    }
}
impl Dispatcher<HostInput, HostEvent> for DigitalNetwork {
    fn dispatch_budget(&self) -> usize {
        self.net.dispatch_budget()
    }
    fn next_due_ns(&self) -> Result<Option<u64>> {
        if let Some(error) = self.net.failure() {
            return Err(error.clone());
        }
        Ok(self.net.next_due_ns())
    }
    fn dispatch(&mut self, cut: u64, budget: usize, context: &HostContext) -> Result<usize> {
        context.check_io()?;
        self.net
            .dispatch(cut, budget, &mut |event| emit(context, event))
            .map_err(|error| context.fail(error))
    }
}
impl Component<HostInput, HostEvent> for DigitalNetwork {
    fn close(&mut self) -> Result<()> {
        self.net.close();
        Ok(())
    }
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        self.net.reset(session)
    }
    fn sampler(&mut self) -> Option<&mut dyn Sampler<HostInput, HostEvent>> {
        Some(self)
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<HostInput, HostEvent>> {
        Some(self)
    }
    fn connection_owner(&mut self) -> Option<&mut dyn ConnectionOwner<HostInput, HostEvent>> {
        Some(self)
    }
}

pub struct DigitalNetFactory;
impl Factory<HostOptions, HostInput, HostEvent> for DigitalNetFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::DigitalNet(options) = &node.options else {
            return Err(admission("net.digital requires digital options"));
        };
        options.validate()?;
        let mut signal = PortSpec::new("signal", "digital");
        signal.category = PortCategory::Signal;
        signal.min_connections = 2;
        signal.max_connections = None;
        Ok(NodeDescription::new(
            vec![signal],
            [
                Capability::Connection,
                Capability::Dispatch,
                Capability::Sample,
            ],
        ))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::DigitalNet(options) = &node.options else {
            return Err(admission("net.digital requires digital options"));
        };
        Ok(Box::new(DigitalNetwork {
            net: DigitalNet::new(options.clone())?,
        }))
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        descriptions: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for owner in &topology.nodes {
            if !matches!(owner.options, HostOptions::DigitalNet(_)) {
                continue;
            }
            let mut drivers = 0;
            let mut sinks = 0;
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
                    if endpoint.port != "signal"
                        || port.protocol != "digital"
                        || port.category != PortCategory::Signal
                    {
                        return Err(admission("digital net requires digital signal endpoints"));
                    }
                    match port.direction {
                        Direction::Out => drivers += 1,
                        Direction::In => sinks += 1,
                        Direction::InOut => {
                            return Err(admission("digital net endpoint must be input or output"));
                        }
                    }
                }
            }
            if drivers != 1 || sinks == 0 {
                return Err(admission(
                    "digital net requires exactly one driver and at least one sink",
                ));
            }
        }
        Ok(())
    }
}
