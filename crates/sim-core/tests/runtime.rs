use sim_core::*;
use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    rc::Rc,
    sync::Arc,
    time::{Duration, Instant},
};

type Log = Rc<RefCell<Vec<String>>>;
type Sim = Simulation<Options, u32, String>;
fn failure(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}
fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}

#[derive(Clone, Copy, Default)]
enum DispatchBehavior {
    #[default]
    Complete,
    Repeat,
    Stall,
    Overreport,
}

#[derive(Clone, Default)]
struct Options {
    capabilities: Vec<Capability>,
    ports: Vec<PortSpec>,
    fail_at: Option<&'static str>,
    wrong_target: bool,
    sample_queues: bool,
    dispatch_behavior: DispatchBehavior,
    fail_target: Option<u64>,
    commit_current: bool,
    close_fails: bool,
    health_fails: Rc<Cell<bool>>,
    returned_interface_missing: bool,
}

struct Probe {
    name: String,
    options: Options,
    log: Log,
    pending: VecDeque<u64>,
}
impl Probe {
    fn record(&self, phase: &str) -> Result<()> {
        self.log.borrow_mut().push(format!("{}:{phase}", self.name));
        if self.options.fail_at == Some(phase) {
            Err(failure(&format!("{} {phase}", self.name)))
        } else {
            Ok(())
        }
    }
}
impl Machine<u32, String> for Probe {
    fn prepare_step(&mut self, _: &StepContext, _: &Context<u32, String>) -> Result<()> {
        self.record("prepare")
    }
    fn request_step(&mut self, _: &StepContext, _: &Context<u32, String>) -> Result<()> {
        self.record("request")
    }
    fn finish_step(
        &mut self,
        step: &StepContext,
        _: &Context<u32, String>,
    ) -> Result<StepResult<String>> {
        self.record("finish")?;
        Ok(StepResult::reached(
            step.t_sim_ns - u64::from(self.options.wrong_target),
        ))
    }
}
impl Stepper<u32, String> for Probe {
    fn step(
        &mut self,
        step: &StepContext,
        context: &Context<u32, String>,
    ) -> Result<StepResult<String>> {
        self.log.borrow_mut().push(format!(
            "{}:{}..{}",
            self.name,
            step.t_sim_ns - step.dt_ns,
            step.t_sim_ns
        ));
        self.record("step")?;
        if self.options.fail_target == Some(step.t_sim_ns) {
            return Err(failure("plant failed after partial effects"));
        }
        context.emit(None, "step", self.name.clone(), EventTime::default())?;
        Ok(StepResult::reached(step.t_sim_ns))
    }
}
impl Input<u32, String> for Probe {
    fn input(&mut self, _: &str, value: u32, context: &Context<u32, String>) -> Result<()> {
        self.log.borrow_mut().push(format!(
            "{}:input:{value}@{}",
            self.name,
            context.t_sim_ns()?
        ));
        Ok(())
    }
}
impl Sampler<u32, String> for Probe {
    fn sample(&mut self, cut: u64, _: &Context<u32, String>) -> Result<()> {
        self.log
            .borrow_mut()
            .push(format!("{}:sample@{cut}", self.name));
        if self.options.sample_queues && cut != 0 {
            self.pending.push_back(cut);
        }
        Ok(())
    }
}
impl InputCommit<u32, String> for Probe {
    fn commit_inputs(&mut self, cut: u64, context: &Context<u32, String>) -> Result<()> {
        self.log
            .borrow_mut()
            .push(format!("{}:commit@{cut}", self.name));
        if self.options.commit_current && cut != 0 {
            context
                .control()
                .schedule_input(ep(&format!("{}.tx", self.name)), 9, Some(cut))?;
        }
        Ok(())
    }
}
impl Dispatcher<u32, String> for Probe {
    fn dispatch_budget(&self) -> usize {
        2
    }
    fn next_due_ns(&self) -> Result<Option<u64>> {
        Ok(self.pending.front().copied())
    }
    fn dispatch(&mut self, cut: u64, budget: usize, _: &Context<u32, String>) -> Result<usize> {
        if self.pending.front().is_some_and(|time| *time <= cut) && budget > 0 {
            if matches!(self.options.dispatch_behavior, DispatchBehavior::Stall) {
                return Ok(0);
            }
            if matches!(self.options.dispatch_behavior, DispatchBehavior::Overreport) {
                return Ok(budget + 1);
            }
            if !matches!(self.options.dispatch_behavior, DispatchBehavior::Repeat) {
                self.pending.pop_front();
            }
            self.log
                .borrow_mut()
                .push(format!("{}:dispatch@{cut}", self.name));
            return Ok(1);
        }
        Ok(0)
    }
}
impl ConnectionOwner<u32, String> for Probe {
    fn attach_port(&mut self, _: Attachment<u32, String>) -> Result<Box<dyn std::any::Any>> {
        Ok(Box::new(()))
    }
}
impl Component<u32, String> for Probe {
    fn close(&mut self) -> Result<()> {
        self.record("close")?;
        if self.options.close_fails {
            Err(failure(&format!("{} cleanup", self.name)))
        } else {
            Ok(())
        }
    }
    fn reset(&mut self, session: Session, _: &Context<u32, String>) -> Result<()> {
        self.log.borrow_mut().push(format!(
            "{}:reset:{}@{}",
            self.name, session.session_id, session.t_sim_ns
        ));
        Ok(())
    }
    fn start(&mut self, _: &Context<u32, String>) -> Result<()> {
        self.record("start")
    }
    fn machine(&mut self) -> Option<&mut dyn Machine<u32, String>> {
        if self.options.returned_interface_missing {
            None
        } else {
            Some(self)
        }
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<u32, String>> {
        Some(self)
    }
    fn input(&mut self) -> Option<&mut dyn Input<u32, String>> {
        Some(self)
    }
    fn sampler(&mut self) -> Option<&mut dyn Sampler<u32, String>> {
        Some(self)
    }
    fn input_commit(&mut self) -> Option<&mut dyn InputCommit<u32, String>> {
        Some(self)
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<u32, String>> {
        Some(self)
    }
    fn connection_owner(&mut self) -> Option<&mut dyn ConnectionOwner<u32, String>> {
        Some(self)
    }
    fn health_check(&self) -> Option<HealthCheck> {
        let bad = self.options.health_fails.clone();
        Some(Rc::new(move || {
            if bad.get() {
                Err(failure("child exited"))
            } else {
                Ok(())
            }
        }))
    }
}
struct ProbeFactory {
    log: Log,
    pure_hooks: Rc<Cell<usize>>,
}
impl InputValidator<Options, u32> for ProbeFactory {
    fn validate_input(
        &self,
        _: &NodeSpec<Options>,
        _: &str,
        value: u32,
        _: &[String],
    ) -> Result<u32> {
        if value == u32::MAX {
            Err(Arc::new(Error::Admission("invalid stimulus".into())))
        } else {
            Ok(value)
        }
    }
}
impl Factory<Options, u32, String> for ProbeFactory {
    fn describe(&self, node: &NodeSpec<Options>) -> Result<NodeDescription> {
        Ok(NodeDescription::new(
            node.options.ports.clone(),
            node.options.capabilities.clone(),
        ))
    }
    fn create(
        &self,
        node: &NodeSpec<Options>,
        context: &mut BuildContext<'_, Options, u32, String>,
    ) -> Result<Box<dyn Component<u32, String>>> {
        self.log.borrow_mut().push(format!("{}:create", node.name));
        if !node.options.capabilities.contains(&Capability::Connection) {
            for port in &node.options.ports {
                if !context.peers(&port.name)?.is_empty() {
                    context.attach::<_, ()>(&port.name, ())?;
                }
            }
        }
        Ok(Box::new(Probe {
            name: node.name.clone(),
            options: node.options.clone(),
            log: self.log.clone(),
            pending: VecDeque::new(),
        }))
    }
    fn input_validator(&self) -> Option<&dyn InputValidator<Options, u32>> {
        Some(self)
    }
    fn validate_topology(
        &self,
        _: &Topology<Options>,
        _: &[NodeDescription],
        _: &Registry<Options, u32, String>,
    ) -> Result<()> {
        self.pure_hooks.set(self.pure_hooks.get() + 1);
        Ok(())
    }
}
fn registry(log: Log) -> (Registry<Options, u32, String>, Rc<Cell<usize>>) {
    let mut registry = Registry::default();
    let hooks = Rc::new(Cell::new(0));
    let id = registry
        .register(
            "probe",
            ProbeFactory {
                log,
                pure_hooks: hooks.clone(),
            },
        )
        .unwrap();
    registry.alias("alias", id).unwrap();
    (registry, hooks)
}
fn node(name: &str, capabilities: Vec<Capability>) -> NodeSpec<Options> {
    NodeSpec::new(
        name,
        "probe",
        Options {
            capabilities,
            ..Default::default()
        },
    )
}
fn input_node(name: &str, extra: Vec<Capability>) -> NodeSpec<Options> {
    let mut node = node(name, extra);
    node.options.capabilities.push(Capability::Input);
    let mut tx = PortSpec::new("tx", "stimulus");
    tx.direction = Direction::In;
    tx.category = PortCategory::Signal;
    tx.capabilities.insert("external_input".into());
    node.options.ports.push(tx);
    node
}
fn simulation(nodes: Vec<NodeSpec<Options>>, options: SimulationOptions) -> (Sim, Log) {
    let log = Log::default();
    let (registry, _) = registry(log.clone());
    (
        Simulation::new(Topology::new(nodes, vec![]).unwrap(), registry, options).unwrap(),
        log,
    )
}
fn options() -> SimulationOptions {
    SimulationOptions {
        quantum_ns: 10,
        ..Default::default()
    }
}

#[test]
fn topology_is_pure_and_construction_is_stable_by_owner_group() {
    let log = Log::default();
    let (registry, hooks) = registry(log.clone());
    let mut owner = node("bus", vec![Capability::Connection]);
    let mut port = PortSpec::new("line", "wire");
    port.max_connections = None;
    owner.options.ports.push(port);
    let mut peer = node("peer", vec![]);
    peer.kind = "alias".into();
    peer.options.ports.push(PortSpec::new("line", "wire"));
    let link = LinkSpec::new(ep("peer.line"), ep("bus.line"));
    let topology = Topology::new(vec![peer.clone(), owner.clone()], vec![link.clone()]).unwrap();
    let mut sim = Simulation::new(topology, registry, options()).unwrap();
    assert!(log.borrow().is_empty());
    assert_eq!(hooks.get(), 1);
    sim.start(None).unwrap();
    assert_eq!(&log.borrow()[..2], ["bus:create", "peer:create"]);
    sim.close().unwrap();
    let (registry, _) = self::registry(log.clone());
    let reversed = LinkSpec::new(link.target.clone(), link.source.clone());
    assert!(Simulation::new(
        Topology::new(vec![peer, owner], vec![link, reversed]).unwrap(),
        registry,
        options()
    )
    .is_err());
    // Public configuration structs are re-admitted at the real boundary.
    let (registry, _) = self::registry(log);
    assert!(Simulation::new(
        Topology {
            nodes: vec![],
            links: vec![]
        },
        registry,
        options()
    )
    .is_err());
}

#[test]
fn machine_phases_are_global_and_preflight_failure_prevents_requests() {
    for failed in [false, true] {
        let a = node("a", vec![Capability::Machine]);
        let mut b = node("b", vec![Capability::Machine]);
        if failed {
            b.options.fail_at = Some("prepare");
        }
        let (mut sim, log) = simulation(vec![a, b], options());
        sim.start(None).unwrap();
        log.borrow_mut().clear();
        let result = sim.step(None, None);
        let expected = if failed {
            vec!["a:prepare", "b:prepare"]
        } else {
            vec![
                "a:prepare",
                "b:prepare",
                "a:request",
                "b:request",
                "a:finish",
                "b:finish",
            ]
        };
        assert_eq!(*log.borrow(), expected);
        assert_eq!(sim.t_sim_ns(), if failed { 0 } else { 10 });
        assert_eq!(result.is_err(), failed);
    }
}

#[test]
fn wrong_completion_stops_later_phases_without_committing_time() {
    let mut a = node("a", vec![Capability::Machine]);
    a.options.wrong_target = true;
    let (mut sim, log) = simulation(
        vec![
            a,
            node("b", vec![Capability::Machine]),
            node("plant", vec![Capability::Step]),
        ],
        options(),
    );
    sim.start(None).unwrap();
    log.borrow_mut().clear();
    assert!(sim.step(None, None).is_err());
    assert_eq!(sim.t_sim_ns(), 0);
    assert_eq!(
        *log.borrow(),
        [
            "a:prepare",
            "b:prepare",
            "a:request",
            "b:request",
            "a:finish"
        ]
    );
}

#[test]
fn all_samples_precede_dispatch_and_input_commit() {
    let mut a = node(
        "a",
        vec![
            Capability::Sample,
            Capability::Dispatch,
            Capability::InputCommit,
        ],
    );
    a.options.sample_queues = true;
    let b = node("b", vec![Capability::Sample, Capability::InputCommit]);
    let (mut sim, log) = simulation(vec![a, b], options());
    sim.start(None).unwrap();
    log.borrow_mut().clear();
    sim.step(None, None).unwrap();
    assert_eq!(
        *log.borrow(),
        [
            "a:commit@0",
            "b:commit@0",
            "a:sample@10",
            "b:sample@10",
            "a:dispatch@10",
            "a:commit@10",
            "b:commit@10"
        ]
    );
}

#[test]
fn off_grid_inputs_and_callback_inputs_keep_stable_cuts_and_ties() {
    let (mut sim, log) = simulation(vec![input_node("host", vec![Capability::Step])], options());
    sim.start(None).unwrap();
    log.borrow_mut().clear();
    sim.schedule_input(ep("host.tx"), 1, Some(3)).unwrap();
    sim.schedule_input(ep("host.tx"), 2, Some(7)).unwrap();
    sim.schedule_input(ep("host.tx"), 3, Some(7)).unwrap();
    sim.set_observer(Some(Rc::new(|event, control| {
        if event.payload == EventPayload::Input(1) {
            control.schedule_input(ep("host.tx"), 4, Some(3))?;
        }
        Ok(())
    })));
    let result = sim.run_until(22, None).unwrap();
    assert_eq!(result.substeps_completed, 4);
    let actual: Vec<_> = log
        .borrow()
        .iter()
        .filter(|entry| entry.contains("..") || entry.contains("input:"))
        .cloned()
        .collect();
    assert_eq!(
        actual,
        [
            "host:0..3",
            "host:input:1@3",
            "host:input:4@3",
            "host:3..7",
            "host:input:2@7",
            "host:input:3@7",
            "host:7..17",
            "host:17..22"
        ]
    );
    let count = log.borrow().len();
    sim.run_until(22, None).unwrap();
    assert_eq!(log.borrow().len(), count);
}

#[test]
fn budget_spans_reentrant_inputs_and_commit_cannot_reopen_cut() {
    let (mut sim, _) = simulation(
        vec![input_node("host", vec![])],
        SimulationOptions {
            dispatch_budget: 3,
            ..options()
        },
    );
    sim.start(None).unwrap();
    sim.set_observer(Some(Rc::new(|event, control| {
        if let EventPayload::Input(value) = event.payload {
            control.schedule_input(ep("host.tx"), value + 1, None)?;
        }
        Ok(())
    })));
    sim.schedule_input(ep("host.tx"), 1, Some(10)).unwrap();
    assert!(sim.step(None, None).is_err());
    assert_eq!(sim.events_total(), 3);
    assert_eq!(sim.t_sim_ns(), 0);
    let mut host = input_node("host", vec![Capability::InputCommit]);
    host.options.commit_current = true;
    let (mut sim, _) = simulation(vec![host], options());
    sim.start(None).unwrap();
    assert!(sim.step(None, None).is_err());
    assert!(sim.failure().unwrap().to_string().contains("commit cut"));
}

#[test]
fn pause_commits_the_current_internal_cut_and_resume_continues() {
    let (mut sim, _) = simulation(vec![node("plant", vec![Capability::Step])], options());
    sim.start(None).unwrap();
    sim.set_observer(Some(Rc::new(|_, control| control.pause())));
    let result = sim.run_until(35, None).unwrap();
    assert_eq!(
        (result.status, result.reached_time_ns),
        (StepStatus::Paused, 10)
    );
    sim.set_observer(None);
    sim.run_until(35, None).unwrap();
    assert_eq!(sim.t_sim_ns(), 35);
}

#[test]
fn first_error_is_immediate_while_paused_and_observation_is_retained() {
    let (mut sim, _) = simulation(vec![node("plant", vec![Capability::Step])], options());
    sim.start(None).unwrap();
    let primary = failure("observer original");
    let supplied = primary.clone();
    sim.set_observer(Some(Rc::new(move |_, control| {
        assert!(Arc::ptr_eq(&control.fail(supplied.clone()), &supplied));
        Err(failure("replacement"))
    })));
    let error = sim.step(None, None).unwrap_err();
    assert!(Arc::ptr_eq(&error, &primary));
    assert_eq!(sim.t_sim_ns(), 0);
    assert_eq!(sim.events_total(), 1);
    assert_eq!(sim.state(), SimulationState::Failed);
    assert!(!sim.failure_notes().is_empty());
    assert!(sim.reset(None).is_err());
    assert!(sim.step(None, None).is_err());
    sim.close().unwrap();
    let (mut paused, _) = simulation(vec![input_node("host", vec![])], options());
    paused.start(None).unwrap();
    let original = failure("transport terminal");
    assert!(Arc::ptr_eq(
        &paused.control().fail(original.clone()),
        &original
    ));
    assert!(Arc::ptr_eq(
        &paused.schedule_input(ep("host.tx"), 1, None).unwrap_err(),
        &original
    ));
}

#[test]
fn reset_recreates_owners_and_invalidates_old_controls_and_typed_handles() {
    let (mut sim, log) = simulation(
        vec![node("plant", vec![Capability::Step])],
        SimulationOptions {
            initial_session: Session::new(8, 50).unwrap(),
            ..options()
        },
    );
    sim.start(None).unwrap();
    let old = sim.control();
    let handle = sim.handle::<Probe>("plant").unwrap();
    sim.step(None, None).unwrap();
    sim.reset(None).unwrap();
    assert_eq!(sim.session(), Some(Session::new(9, 50).unwrap()));
    assert!(sim.component(&handle).is_err());
    assert!(old.check_io().is_err());
    let supplied = failure("old callback");
    assert!(Arc::ptr_eq(&old.fail(supplied.clone()), &supplied));
    assert!(sim.failure().is_none());
    let creates = log
        .borrow()
        .iter()
        .filter(|entry| entry.ends_with(":create"))
        .count();
    assert_eq!(creates, 2);
    let current = sim.handle::<Probe>("plant").unwrap();
    let (mut other, _) = simulation(vec![node("plant", vec![Capability::Step])], options());
    other.start(None).unwrap();
    assert!(other.component(&current).is_err());
}

#[test]
fn bounded_history_preserves_owned_payload_and_three_times_for_observer() {
    let (mut sim, _) = simulation(
        vec![node("plant", vec![])],
        SimulationOptions {
            event_history: 2,
            ..options()
        },
    );
    sim.start(None).unwrap();
    let seen = Rc::new(RefCell::new(vec![]));
    let captured = seen.clone();
    sim.set_observer(Some(Rc::new(move |event, _| {
        captured.borrow_mut().push(event.clone());
        Ok(())
    })));
    let handle = sim.handle::<Probe>("plant").unwrap();
    for n in 0..4 {
        sim.with_component(&handle, |_, context| {
            context.emit(
                None,
                "sample",
                format!("sample {n}"),
                EventTime {
                    source_time_ns: Some(1),
                    mapped_time_ns: Some(2),
                    delivery_time_ns: Some(3),
                    quality: Some(TimeQuality::UpperBound),
                },
            )?;
            Ok(())
        })
        .unwrap();
    }
    assert_eq!((sim.events_total(), sim.events_dropped()), (4, 2));
    assert_eq!(sim.events()[0].sequence, 2);
    assert_eq!(seen.borrow().len(), 4);
    let event = &seen.borrow()[0];
    assert_eq!(
        (
            event.source_time_ns,
            event.mapped_time_ns,
            event.delivery_time_ns,
            event.quality
        ),
        (Some(1), 2, 3, TimeQuality::UpperBound)
    );
    assert_eq!(event.payload, EventPayload::Observation("sample 0".into()));
}

#[test]
fn deadlines_health_and_cleanup_preserve_first_failure_and_reverse_order() {
    let health = Rc::new(Cell::new(false));
    let mut a = node("a", vec![Capability::Health]);
    a.options.health_fails = health.clone();
    a.options.close_fails = true;
    let mut b = node("b", vec![]);
    b.options.close_fails = true;
    let (mut sim, log) = simulation(vec![a, b], options());
    let deadline = Instant::now() + Duration::from_secs(5);
    sim.start(Some(deadline)).unwrap();
    assert!(sim.step(None, None).is_err());
    assert_eq!(sim.state(), SimulationState::Paused);
    health.set(true);
    let error = sim.check_deadline().unwrap_err();
    assert_eq!(error.to_string(), "child exited");
    assert!(Arc::ptr_eq(&sim.close().unwrap_err(), &error));
    assert_eq!(
        &log.borrow()[log.borrow().len() - 2..],
        ["b:close", "a:close"]
    );
    assert_eq!(sim.failure_notes().len(), 2);
    sim.close().unwrap();
    let (mut expired, log) = simulation(vec![node("never", vec![])], options());
    assert!(expired.start(Some(Instant::now())).is_err());
    assert!(log.borrow().is_empty());
}

#[test]
fn returned_bad_interface_is_registered_for_cleanup_and_overflow_is_pure() {
    let mut bad = node("bad", vec![Capability::Machine]);
    bad.options.returned_interface_missing = true;
    let (mut sim, log) = simulation(vec![bad], options());
    assert!(sim.start(None).is_err());
    assert_eq!(*log.borrow(), ["bad:create", "bad:close"]);
    let (mut sim, _) = simulation(
        vec![node("plant", vec![])],
        SimulationOptions {
            initial_session: Session::new(1, u64::MAX).unwrap(),
            ..options()
        },
    );
    sim.start(None).unwrap();
    assert!(sim.step(Some(1), None).is_err());
    assert_eq!(sim.state(), SimulationState::Paused);
    assert!(sim.failure().is_none());
}

#[test]
fn dispatcher_budget_is_not_refreshed_and_bad_progress_reports_freeze() {
    for (behavior, message) in [
        (DispatchBehavior::Repeat, "budget exhausted"),
        (DispatchBehavior::Stall, "no progress"),
        (DispatchBehavior::Overreport, "exceeding its budget"),
    ] {
        let mut dispatcher = node("fabric", vec![Capability::Sample, Capability::Dispatch]);
        dispatcher.options.sample_queues = true;
        dispatcher.options.dispatch_behavior = behavior;
        let (mut sim, log) = simulation(vec![dispatcher], options());
        sim.start(None).unwrap();
        let error = sim.step(None, None).unwrap_err();
        assert!(error.to_string().contains(message), "{error}");
        assert_eq!(sim.t_sim_ns(), 0);
        if matches!(behavior, DispatchBehavior::Repeat) {
            assert_eq!(
                log.borrow()
                    .iter()
                    .filter(|entry| entry.as_str() == "fabric:dispatch@10")
                    .count(),
                2
            );
        }
    }
}

#[test]
fn later_interval_failure_retains_first_commit_and_partial_effects() {
    let mut plant = node("plant", vec![Capability::Step]);
    plant.options.fail_target = Some(20);
    let (mut sim, log) = simulation(vec![plant], options());
    sim.start(None).unwrap();
    assert!(sim.run_until(30, None).is_err());
    assert_eq!((sim.t_sim_ns(), sim.step_id()), (10, 1));
    assert!(log.borrow().iter().any(|entry| entry == "plant:10..20"));
    assert_eq!(sim.events_total(), 1);
}

#[test]
fn expired_active_deadline_prevents_borrowed_component_effects_and_drop_cleans() {
    let (mut sim, log) = simulation(vec![node("owned", vec![])], options());
    sim.start(Some(Instant::now() + Duration::from_secs(10)))
        .unwrap();
    let handle = sim.handle::<Probe>("owned").unwrap();
    assert!(sim.step(None, Some(Instant::now())).is_err());
    let called = Cell::new(false);
    assert!(sim
        .with_component(&handle, |_, _| {
            called.set(true);
            Ok(())
        })
        .is_err());
    assert!(!called.get());
    drop(sim);
    assert_eq!(log.borrow().last().unwrap(), "owned:close");
}
