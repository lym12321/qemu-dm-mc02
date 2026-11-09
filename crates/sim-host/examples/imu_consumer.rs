//! A compiled SI consumer borrowing completed samples through net.imu.
use sim_core::{
    BuildContext, Component, Direction, Endpoint, Factory, LinkSpec, NodeDescription, NodeSpec,
    PortCategory, PortSpec, Result, SimulationOptions, Topology,
};
use sim_host::{
    config::scene::load_scene,
    fabric::imu::ImuAttachment,
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
    signals::ImuSignalSample,
};
use std::{cell::RefCell, path::PathBuf, rc::Rc};

struct Monitor;
impl Component<HostInput, HostEvent> for Monitor {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
}
struct MonitorFactory(Rc<RefCell<Vec<ImuSignalSample>>>);
impl Factory<HostOptions, HostInput, HostEvent> for MonitorFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("imu", "imu");
        port.direction = Direction::In;
        port.category = PortCategory::Signal;
        port.min_connections = 1;
        Ok(NodeDescription::new(vec![port], []))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let samples = self.0.clone();
        build.attach::<_, ()>(
            "imu",
            ImuAttachment::Sink(Rc::new(move |sample| {
                samples.borrow_mut().push(sample);
                Ok(())
            })),
        )?;
        Ok(Box::new(Monitor))
    }
}
fn main() -> Result<()> {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/shared-world/imu.yaml");
    let scene =
        load_scene(path).map_err(|error| -> sim_core::Failure { std::sync::Arc::new(error) })?;
    let samples = Rc::new(RefCell::new(vec![]));
    let mut registry = runtime::registry()?;
    registry.register("example.imu-monitor", MonitorFactory(samples.clone()))?;
    let mut nodes = scene.topology.nodes;
    nodes.extend([
        NodeSpec::new("wire", "net.imu", HostOptions::ImuNet(Default::default())),
        NodeSpec::new("monitor", "example.imu-monitor", HostOptions::CanHost),
    ]);
    let mut simulation = HostSimulation::new(
        Topology::new(
            nodes,
            vec![
                LinkSpec::new(
                    Endpoint::parse("world.imu.body")?,
                    Endpoint::parse("wire.signal")?,
                ),
                LinkSpec::new(
                    Endpoint::parse("monitor.imu")?,
                    Endpoint::parse("wire.signal")?,
                ),
            ],
        )?,
        registry,
        SimulationOptions {
            mode: scene.mode,
            quantum_ns: scene.quantum_ns,
            ..Default::default()
        },
    )?;
    let execution = (|| {
        simulation.start(None)?;
        for (at, effort) in [(0, 0.4), (10_000_000, 0.0)] {
            simulation.schedule_input(
                Endpoint::parse("world.effort")?,
                HostInput::JointEffort(vec![("shaft".into(), effort)]),
                Some(at),
            )?;
        }
        simulation.run_until(scene.duration_ns, None)?;
        Ok(())
    })();
    let cleanup = simulation.close();
    if let Err(error) = &cleanup {
        eprintln!("cleanup: {error}");
    }
    execution.and(cleanup)?;
    for sample in samples.borrow().iter() {
        println!("{sample:?}");
    }
    Ok(())
}
