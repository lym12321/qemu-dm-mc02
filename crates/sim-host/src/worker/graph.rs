//! One core scheduler for legacy commands; native integration occurs only on
//! the explicit Advance input, retaining the backend's exact floating dt.
use super::{
    engine::{EngineSample, WorkerEngine},
    motor_protocol::MotorCommand,
};
use sim_core::*;
use std::{
    cell::RefCell,
    rc::Rc,
    sync::{atomic::AtomicBool, Arc},
    time::Instant,
};

#[derive(Clone)]
enum EngineInput {
    Command(MotorCommand),
    Advance(f64),
}
type BackendSlot = Rc<RefCell<Option<Box<dyn WorkerEngine>>>>;
type SampleSlot = Rc<RefCell<Option<EngineSample>>>;
type Graph = Simulation<(), EngineInput, ()>;
struct EngineFactory {
    backend: BackendSlot,
    sample: SampleSlot,
}
struct EngineOwner {
    backend: Option<Box<dyn WorkerEngine>>,
    slot: BackendSlot,
    sample: SampleSlot,
}
fn invalid(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
impl Factory<(), EngineInput, ()> for EngineFactory {
    fn describe(&self, _: &NodeSpec<()>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("input", "worker.engine");
        port.direction = Direction::In;
        port.capabilities.insert("external_input".into());
        Ok(NodeDescription::new(vec![port], [Capability::Input]))
    }
    fn create(
        &self,
        _: &NodeSpec<()>,
        _: &mut BuildContext<'_, (), EngineInput, ()>,
    ) -> Result<Box<dyn Component<EngineInput, ()>>> {
        let backend = self
            .backend
            .borrow_mut()
            .take()
            .ok_or_else(|| invalid("worker backend already acquired"))?;
        Ok(Box::new(EngineOwner {
            backend: Some(backend),
            slot: self.backend.clone(),
            sample: self.sample.clone(),
        }))
    }
    fn input_validator(&self) -> Option<&dyn InputValidator<(), EngineInput>> {
        Some(self)
    }
}
impl InputValidator<(), EngineInput> for EngineFactory {
    fn validate_input(
        &self,
        _: &NodeSpec<()>,
        port: &str,
        value: EngineInput,
        _: &[String],
    ) -> Result<EngineInput> {
        if port != "input" {
            return Err(invalid("unknown worker engine input"));
        }
        if let EngineInput::Advance(dt) = value {
            if !dt.is_finite() || dt <= 0.0 {
                return Err(invalid("native dt must be finite and positive"));
            }
        }
        Ok(value)
    }
}
impl Component<EngineInput, ()> for EngineOwner {
    fn reset(&mut self, _: Session, _: &Context<EngineInput, ()>) -> Result<()> {
        self.sample.borrow_mut().take();
        self.backend.as_mut().expect("owned backend").reset()
    }
    fn close(&mut self) -> Result<()> {
        if let Some(backend) = self.backend.take() {
            *self.slot.borrow_mut() = Some(backend);
        }
        Ok(())
    }
    fn input(&mut self) -> Option<&mut dyn Input<EngineInput, ()>> {
        Some(self)
    }
}
impl Input<EngineInput, ()> for EngineOwner {
    fn input(&mut self, _: &str, value: EngineInput, _: &Context<EngineInput, ()>) -> Result<()> {
        let backend = self.backend.as_mut().expect("owned backend");
        match value {
            EngineInput::Command(c) => backend.command(c),
            EngineInput::Advance(dt) => {
                let sample = backend.step(dt)?;
                sample.imu.validate()?;
                if sample
                    .motors
                    .iter()
                    .any(|(p, v, t)| !p.is_finite() || !v.is_finite() || !t.is_finite())
                {
                    return Err(invalid("backend motor sample must be finite"));
                }
                *self.sample.borrow_mut() = Some(sample);
                Ok(())
            }
        }
    }
}

pub struct LegacyGraph {
    graph: Graph,
    backend: BackendSlot,
    sample: SampleSlot,
    cancellation: Option<Arc<AtomicBool>>,
    closed: bool,
}
impl LegacyGraph {
    pub fn new(
        backend: Box<dyn WorkerEngine>,
        session: Session,
        deadline: Instant,
        cancellation: Option<Arc<AtomicBool>>,
    ) -> Result<Self> {
        let backend = Rc::new(RefCell::new(Some(backend)));
        let sample = Rc::new(RefCell::new(None));
        let graph = Self::build(
            backend.clone(),
            sample.clone(),
            session,
            cancellation.clone(),
        )?;
        let mut owner = Self {
            graph,
            backend,
            sample,
            cancellation,
            closed: false,
        };
        if let Err(primary) = owner.graph.start(Some(deadline)) {
            let _ = owner.close();
            return Err(primary);
        }
        Ok(owner)
    }
    fn build(
        backend: BackendSlot,
        sample: SampleSlot,
        session: Session,
        cancellation: Option<Arc<AtomicBool>>,
    ) -> Result<Graph> {
        let topology = Topology::new(vec![NodeSpec::new("engine", "worker.engine", ())], vec![])?;
        let mut registry = Registry::default();
        registry.register("worker.engine", EngineFactory { backend, sample })?;
        Simulation::new(
            topology,
            registry,
            SimulationOptions {
                initial_session: session,
                quantum_ns: u64::MAX,
                max_pending_inputs: 4096,
                event_history: 1,
                cancellation,
                ..Default::default()
            },
        )
    }
    pub fn submit(&self, c: MotorCommand) -> Result<()> {
        self.graph.schedule_input(
            Endpoint::new("engine", "input")?,
            EngineInput::Command(c),
            Some(c.timestamp_ns.max(self.graph.t_sim_ns())),
        )
    }
    pub fn advance(&mut self, dt: f64, deadline: Instant) -> Result<EngineSample> {
        self.graph.schedule_input(
            Endpoint::new("engine", "input")?,
            EngineInput::Advance(dt),
            None,
        )?;
        self.graph
            .run_until(self.graph.t_sim_ns(), Some(deadline))?;
        self.sample
            .borrow()
            .clone()
            .ok_or_else(|| invalid("worker backend did not publish completed sample"))
    }
    pub fn reach(&mut self, time: u64, deadline: Instant) -> Result<()> {
        self.graph.run_until(time, Some(deadline))?;
        Ok(())
    }
    pub fn rebase(&mut self, session: Session, deadline: Instant) -> Result<()> {
        self.graph.close()?;
        self.graph = Self::build(
            self.backend.clone(),
            self.sample.clone(),
            session,
            self.cancellation.clone(),
        )?;
        self.graph.start(Some(deadline))
    }
    pub fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        let mut primary = self.graph.close().err();
        if let Some(mut backend) = self.backend.borrow_mut().take() {
            if let Err(e) = backend.close() {
                if primary.is_none() {
                    primary = Some(e)
                }
            }
        }
        primary.map_or(Ok(()), Err)
    }
}
impl Drop for LegacyGraph {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
