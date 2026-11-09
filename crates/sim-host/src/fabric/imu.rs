//! Completed site-local SI IMU broadcasts; every cut is delivered, including equal values.
//! Source timestamps are metadata, without sensor identity or consumption claims.

pub use super::sampled::NetOptions as ImuNetOptions;
use super::sampled::{self, Fanout, Policy};
use crate::{
    runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry},
    signals::ImuSignalSample,
};
use sim_core::{
    Attachment, BuildContext, Capability, Component, ConnectionOwner, Direction, Dispatcher, Error,
    EventTime, Factory, Failure, NodeDescription, NodeSpec, PortCategory, PortSpec, Result,
    Sampler, Session, TimeQuality, Topology,
};
use std::{any::Any, rc::Rc, sync::Arc};

fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
pub type ImuSource = Rc<dyn Fn() -> Result<ImuSignalSample>>;
pub type ImuSink = Rc<dyn Fn(ImuSignalSample) -> Result<()>>;
/// Both callbacks borrow completed endpoint state; closing the net never closes endpoints.
pub enum ImuAttachment {
    Source(ImuSource),
    Sink(ImuSink),
}
#[derive(Clone, Debug, PartialEq)]
pub enum ImuObservation {
    Sample {
        source: String,
        sample: ImuSignalSample,
        cut_ns: u64,
    },
    Delivery {
        source: String,
        destination: String,
        sample: ImuSignalSample,
        sequence: u64,
        due_ns: u64,
        cut_ns: u64,
    },
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ImuState {
    pub sample: Option<ImuSignalSample>,
    pub sample_time_ns: Option<u64>,
    pub pending: usize,
    pub failed: bool,
}
#[derive(Default)]
struct ImuPolicy {
    last_source_time_ns: Option<u64>,
}
impl Policy for ImuPolicy {
    const NAME: &'static str = "imu";
    type Sample = ImuSignalSample;
    type Delivery = ImuSignalSample;
    fn validate(
        &self,
        sample: ImuSignalSample,
        _previous: Option<ImuSignalSample>,
        cut: u64,
    ) -> Result<bool> {
        sample.validate()?;
        if sample.mapped_time_ns != cut {
            return Err(admission("imu sample must match the completed cut"));
        }
        if sample
            .source_time_ns
            .zip(self.last_source_time_ns)
            .is_some_and(|(now, previous)| now < previous)
        {
            return Err(admission("imu sample time must not decrease"));
        }
        Ok(true)
    }
    fn commit(&mut self, sample: ImuSignalSample) {
        if sample.source_time_ns.is_some() {
            self.last_source_time_ns = sample.source_time_ns;
        }
    }
    fn delivery(sample: ImuSignalSample) -> ImuSignalSample {
        sample
    }
}
impl From<sampled::Observation<ImuSignalSample>> for ImuObservation {
    fn from(value: sampled::Observation<ImuSignalSample>) -> Self {
        match value {
            sampled::Observation::Sample {
                source,
                sample,
                cut_ns,
                ..
            } => Self::Sample {
                source,
                sample,
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
/// SI validation policy over the same bounded fanout owner as DigitalNet.
pub struct ImuNet {
    fanout: Fanout<ImuPolicy>,
}
impl ImuNet {
    pub fn new(options: ImuNetOptions) -> Result<Self> {
        Ok(Self {
            fanout: Fanout::new(options)?,
        })
    }
    pub fn attach(&mut self, name: String, endpoint: ImuAttachment) -> Result<()> {
        self.fanout.attach(
            name,
            match endpoint {
                ImuAttachment::Source(read) => sampled::Attachment::Source(read),
                ImuAttachment::Sink(receive) => sampled::Attachment::Sink(receive),
            },
        )
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        self.fanout.reset(session)
    }
    pub fn failure(&self) -> Option<&Failure> {
        self.fanout.failure()
    }
    pub fn observe(&self) -> ImuState {
        let state = self.fanout.observe();
        ImuState {
            sample: state.sample,
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
        observe: &mut impl FnMut(ImuObservation) -> Result<()>,
    ) -> Result<()> {
        self.fanout.sample(cut, &mut |event| observe(event.into()))
    }
    pub fn dispatch(
        &mut self,
        cut: u64,
        budget: usize,
        observe: &mut impl FnMut(ImuObservation) -> Result<()>,
    ) -> Result<usize> {
        self.fanout
            .dispatch(cut, budget, &mut |event| observe(event.into()))
    }
    pub fn close(&mut self) {
        self.fanout.close();
    }
}
fn emit(context: &HostContext, observation: ImuObservation) -> Result<()> {
    let (event, sample, cut, kind) = match observation {
        ImuObservation::Sample { sample, cut_ns, .. } => (
            HostEvent::ImuSample { sample },
            sample,
            cut_ns,
            "net.imu.sample",
        ),
        ImuObservation::Delivery {
            source,
            destination,
            sample,
            sequence,
            due_ns,
            cut_ns,
        } => (
            HostEvent::ImuDelivery {
                source,
                destination,
                sample,
                sequence,
                due_ns,
            },
            sample,
            cut_ns,
            "net.imu.delivery",
        ),
    };
    context.emit(
        Some("signal"),
        kind,
        event,
        EventTime {
            source_time_ns: sample.source_time_ns,
            mapped_time_ns: Some(sample.mapped_time_ns),
            delivery_time_ns: Some(cut),
            quality: Some(TimeQuality::Sampled),
        },
    )?;
    context.check_io()?;
    context.check_deadline()
}
pub struct ImuNetwork {
    net: ImuNet,
}
impl ImuNetwork {
    pub fn observe(&self) -> ImuState {
        self.net.observe()
    }
}
impl ConnectionOwner<HostInput, HostEvent> for ImuNetwork {
    fn attach_port(
        &mut self,
        attachment: Attachment<HostInput, HostEvent>,
    ) -> Result<Box<dyn Any>> {
        let descriptor = &attachment.descriptor;
        if attachment.owner_port != "signal"
            || descriptor.protocol != "imu"
            || descriptor.category != PortCategory::Signal
            || !matches!(descriptor.direction, Direction::In | Direction::Out)
        {
            return Err(admission(
                "imu endpoint must be an input or output imu signal",
            ));
        }
        let endpoint = *attachment
            .endpoint
            .downcast::<ImuAttachment>()
            .map_err(|_| admission("imu connection requires a typed imu attachment"))?;
        let context = attachment.context;
        let endpoint = match (descriptor.direction, endpoint) {
            (Direction::Out, ImuAttachment::Source(read)) => {
                ImuAttachment::Source(Rc::new(move || {
                    context.check_io()?;
                    context.check_deadline()?;
                    let sample = read()?;
                    context.check_io()?;
                    context.check_deadline()?;
                    Ok(sample)
                }))
            }
            (Direction::In, ImuAttachment::Sink(receive)) => {
                ImuAttachment::Sink(Rc::new(move |level| {
                    context.check_io()?;
                    context.check_deadline()?;
                    receive(level)?;
                    context.check_io()?;
                    context.check_deadline()
                }))
            }
            _ => {
                return Err(admission("imu attachment role must match port direction"));
            }
        };
        self.net.attach(attachment.endpoint_name, endpoint)?;
        Ok(Box::new(()))
    }
}
impl Sampler<HostInput, HostEvent> for ImuNetwork {
    fn sample(&mut self, cut: u64, context: &HostContext) -> Result<()> {
        context.check_io()?;
        self.net
            .sample(cut, &mut |event| emit(context, event))
            .map_err(|error| context.fail(error))
    }
}
impl Dispatcher<HostInput, HostEvent> for ImuNetwork {
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
impl Component<HostInput, HostEvent> for ImuNetwork {
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

pub struct ImuNetFactory;
impl Factory<HostOptions, HostInput, HostEvent> for ImuNetFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::ImuNet(options) = &node.options else {
            return Err(admission("net.imu requires imu options"));
        };
        options.validate()?;
        let mut signal = PortSpec::new("signal", "imu");
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
        let HostOptions::ImuNet(options) = &node.options else {
            return Err(admission("net.imu requires imu options"));
        };
        Ok(Box::new(ImuNetwork {
            net: ImuNet::new(options.clone())?,
        }))
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        descriptions: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for owner in &topology.nodes {
            if !matches!(owner.options, HostOptions::ImuNet(_)) {
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
                        || port.protocol != "imu"
                        || port.category != PortCategory::Signal
                    {
                        return Err(admission("imu net requires imu signal endpoints"));
                    }
                    match port.direction {
                        Direction::Out => drivers += 1,
                        Direction::In => sinks += 1,
                        Direction::InOut => {
                            return Err(admission("imu net endpoint must be input or output"));
                        }
                    }
                }
            }
            if drivers != 1 || sinks == 0 {
                return Err(admission(
                    "imu net requires exactly one driver and at least one sink",
                ));
            }
        }
        Ok(())
    }
}
