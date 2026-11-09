//! Four explicit edges within one Simulation interval; no GPIO waveform claim.
use sim_core::{
    BuildContext, Capability, Component, Direction, Endpoint, Factory, LinkSpec, NodeDescription,
    NodeSpec, PortCategory, PortSpec, Result, SimulationOptions, StepContext, StepResult, Stepper,
    Topology,
};
use sim_host::{
    fabric::edge::{DigitalEdge, EdgeAttachment, EdgeOutput},
    runtime::{self, HostContext, HostEvent, HostInput, HostOptions, HostSimulation},
};
use std::{cell::RefCell, rc::Rc};

struct Source {
    output: EdgeOutput,
    sent: bool,
}
impl Component<HostInput, HostEvent> for Source {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        Some(self)
    }
}
impl Stepper<HostInput, HostEvent> for Source {
    fn step(&mut self, step: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        if !self.sent {
            for (mapped_time_ns, level) in [(100, true), (101, false), (101, true), (999, false)] {
                self.output.send(DigitalEdge {
                    level,
                    source_time_ns: 5000 + mapped_time_ns,
                    mapped_time_ns,
                })?;
            }
            self.sent = true;
        }
        Ok(StepResult::reached(step.t_sim_ns))
    }
}
struct SourceFactory;
fn port(name: &str, direction: Direction) -> PortSpec {
    let mut port = PortSpec::new(name, "digital-edge");
    port.direction = direction;
    port.category = PortCategory::Signal;
    port
}
impl Factory<HostOptions, HostInput, HostEvent> for SourceFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut output = port("out", Direction::Out);
        output.capabilities.insert("event-output".into());
        Ok(NodeDescription::new(vec![output], [Capability::Step]))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        Ok(Box::new(Source {
            output: build.attach("out", EdgeAttachment::Source)?,
            sent: false,
        }))
    }
}
struct Receiver;
impl Component<HostInput, HostEvent> for Receiver {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
}
type Received = Rc<RefCell<Vec<(DigitalEdge, u64)>>>;
struct ReceiverFactory(Received);
impl Factory<HostOptions, HostInput, HostEvent> for ReceiverFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        Ok(NodeDescription::new(vec![port("input", Direction::In)], []))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let received = self.0.clone();
        let context = build.context();
        build.attach::<_, ()>(
            "input",
            EdgeAttachment::Sink(Rc::new(move |edge| {
                received.borrow_mut().push((edge, context.t_sim_ns()?));
                Ok(())
            })),
        )?;
        Ok(Box::new(Receiver))
    }
}
fn main() -> Result<()> {
    let received = Received::default();
    let mut registry = runtime::registry()?;
    registry.register("example.source", SourceFactory)?;
    registry.register("example.receiver", ReceiverFactory(received.clone()))?;
    let mut simulation = HostSimulation::new(
        Topology::new(
            vec![
                NodeSpec::new("source", "example.source", HostOptions::CanHost),
                NodeSpec::new(
                    "wire",
                    "net.digital-edge",
                    HostOptions::DigitalEdgeNet(Default::default()),
                ),
                NodeSpec::new("sink", "example.receiver", HostOptions::CanHost),
            ],
            vec![
                LinkSpec::new(
                    Endpoint::parse("source.out")?,
                    Endpoint::parse("wire.signal")?,
                ),
                LinkSpec::new(
                    Endpoint::parse("sink.input")?,
                    Endpoint::parse("wire.signal")?,
                ),
            ],
        )?,
        registry,
        SimulationOptions {
            quantum_ns: 1_000_000,
            ..Default::default()
        },
    )?;
    let execution = (|| {
        simulation.start(None)?;
        simulation.step(None, None)?;
        Ok(())
    })();
    let cleanup = simulation.close();
    if let Err(error) = &cleanup {
        eprintln!("cleanup: {error}");
    }
    execution.and(cleanup)?;
    println!("scripted Rust events: {:?}", received.borrow());
    Ok(())
}
