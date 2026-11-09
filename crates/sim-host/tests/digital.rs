#![cfg(feature = "simulation")]

use sim_core::{
    BuildContext, Capability, Component, Direction, Endpoint, EventPayload, Factory, Failure,
    LinkSpec, NodeDescription, NodeSpec, PortCategory, PortSpec, Result, Sampler, Session,
    SimulationOptions, SimulationState, StepContext, StepResult, Stepper, TimeQuality, Topology,
};
use sim_host::{
    fabric::digital::{
        DigitalAttachment, DigitalNet, DigitalNetOptions, DigitalNetwork, DigitalObservation,
        DigitalSample,
    },
    runtime::{registry, HostContext, HostEvent, HostInput, HostOptions, HostSimulation},
};
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
};

fn sample(level: Option<bool>, source_time_ns: u64, mapped_time_ns: u64) -> DigitalSample {
    DigitalSample {
        level,
        source_time_ns,
        mapped_time_ns,
    }
}
fn no_events(_: DigitalObservation) -> Result<()> {
    Ok(())
}

type Writes = Rc<RefCell<Vec<(usize, bool)>>>;
fn wired(
    options: DigitalNetOptions,
    count: usize,
) -> (DigitalNet, Rc<Cell<DigitalSample>>, Rc<Cell<usize>>, Writes) {
    let mut net = DigitalNet::new(options).unwrap();
    let value = Rc::new(Cell::new(sample(None, 100, 0)));
    let reads = Rc::new(Cell::new(0));
    let (source_value, source_reads) = (value.clone(), reads.clone());
    net.attach(
        "source.pin".into(),
        DigitalAttachment::Source(Rc::new(move || {
            source_reads.set(source_reads.get() + 1);
            Ok(source_value.get())
        })),
    )
    .unwrap();
    let writes = Rc::new(RefCell::new(vec![]));
    for index in 0..count {
        let writes = writes.clone();
        net.attach(
            format!("sink{index}.pin"),
            DigitalAttachment::Sink(Rc::new(move |level| {
                writes.borrow_mut().push((index, level));
                Ok(())
            })),
        )
        .unwrap();
    }
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    (net, value, reads, writes)
}

#[test]
fn unavailable_initial_drive_and_changed_levels_keep_sample_and_broadcast_phases_separate() {
    let (mut net, value, reads, writes) = wired(DigitalNetOptions::default(), 2);
    let mut events = vec![];
    net.sample(0, &mut |event| {
        events.push(event);
        Ok(())
    })
    .unwrap();
    assert_eq!(net.observe().level, None);
    assert_eq!(net.dispatch(0, 1, &mut no_events).unwrap(), 0);
    assert!(writes.borrow().is_empty());
    assert_eq!(
        events,
        [DigitalObservation::Sample {
            source: "source.pin".into(),
            sample: sample(None, 100, 0),
            changed: false,
            cut_ns: 0,
        }]
    );

    value.set(sample(Some(false), 110, 10));
    net.sample(10, &mut no_events).unwrap();
    assert!(writes.borrow().is_empty());
    assert_eq!(net.next_due_ns(), Some(10));
    assert_eq!(net.dispatch(10, 0, &mut no_events).unwrap(), 0);
    events.clear();
    assert_eq!(
        net.dispatch(10, 1, &mut |event| {
            events.push(event);
            Ok(())
        })
        .unwrap(),
        1
    );
    assert_eq!(*writes.borrow(), [(0, false), (1, false)]);
    assert_eq!(
        events[0],
        DigitalObservation::Delivery {
            source: "source.pin".into(),
            destination: "sink0.pin".into(),
            sample: sample(Some(false), 110, 10),
            sequence: 0,
            due_ns: 10,
            cut_ns: 10,
        }
    );
    value.set(sample(Some(false), 120, 20));
    net.sample(20, &mut no_events).unwrap();
    assert_eq!(net.next_due_ns(), None);
    value.set(sample(Some(true), 130, 30));
    net.sample(30, &mut no_events).unwrap();
    assert_eq!(net.dispatch(30, 10, &mut no_events).unwrap(), 1);
    assert!(net
        .sample(30, &mut no_events)
        .unwrap_err()
        .to_string()
        .contains("once per completed cut"));
    assert_eq!(reads.get(), 4);
    assert_eq!(
        *writes.borrow(),
        [(0, false), (1, false), (0, true), (1, true)]
    );
    net.close();
    net.close();
}

#[test]
fn broadcast_capacity_budget_and_reset_preserve_committed_sample() {
    let (mut net, value, reads, writes) = wired(
        DigitalNetOptions {
            max_pending: 2,
            dispatch_budget: 1,
        },
        2,
    );
    for (cut, level) in [(0, false), (1, true)] {
        value.set(sample(Some(level), 100 + cut, cut));
        net.sample(cut, &mut no_events).unwrap();
    }
    assert_eq!(net.dispatch(2, 99, &mut no_events).unwrap(), 1);
    assert_eq!(*writes.borrow(), [(0, false), (1, false)]);
    assert_eq!(net.next_due_ns(), Some(1));
    assert_eq!(net.dispatch(2, 99, &mut no_events).unwrap(), 1);

    net.reset(Session::new(2, 10).unwrap()).unwrap();
    assert_eq!(reads.get(), 2);
    for (cut, level) in [(10, false), (11, true), (12, false)] {
        value.set(sample(Some(level), 100 + cut, cut));
        let result = net.sample(cut, &mut no_events);
        if cut < 12 {
            result.unwrap();
        } else {
            assert!(result.unwrap_err().to_string().contains("capacity"));
        }
    }
    let state = net.observe();
    assert_eq!(
        (
            state.pending,
            state.level,
            state.sample_time_ns,
            state.failed
        ),
        (2, Some(true), Some(11), true)
    );
    let failure = net.failure().unwrap().clone();
    assert!(Arc::ptr_eq(
        &failure,
        &net.dispatch(12, 1, &mut no_events).unwrap_err()
    ));
    net.reset(Session::new(3, 20).unwrap()).unwrap();
    assert_eq!(
        (reads.get(), net.observe().failed, net.next_due_ns()),
        (5, false, None)
    );
}

#[test]
fn invalid_observations_and_released_drive_freeze_before_any_new_delivery() {
    for (invalid, message) in [
        (sample(Some(true), 101, 11), "beyond"),
        (sample(Some(true), 99, 10), "must not decrease"),
        (sample(Some(true), 101, 0), "must not decrease"),
        (sample(None, 110, 10), "lost output drive"),
    ] {
        let (mut net, value, reads, writes) = wired(DigitalNetOptions::default(), 1);
        value.set(sample(Some(false), 100, 1));
        net.sample(1, &mut no_events).unwrap();
        net.dispatch(1, 1, &mut no_events).unwrap();
        value.set(invalid);
        assert!(net
            .sample(10, &mut no_events)
            .unwrap_err()
            .to_string()
            .contains(message));
        assert_eq!(
            (net.observe().level, net.observe().sample_time_ns),
            (Some(false), Some(1))
        );
        assert!(net.sample(20, &mut no_events).is_err());
        assert_eq!((reads.get(), &*writes.borrow()), (2, &vec![(0, false)]));
    }
}

#[test]
fn sink_failure_keeps_partial_effects_and_never_replays_the_popped_broadcast() {
    let mut net = DigitalNet::new(DigitalNetOptions::default()).unwrap();
    net.attach(
        "source.pin".into(),
        DigitalAttachment::Source(Rc::new(|| Ok(sample(Some(true), 100, 0)))),
    )
    .unwrap();
    let writes = Rc::new(RefCell::new(vec![]));
    let failure: Failure = Arc::new(std::io::Error::other("sink refused level"));
    for index in 0..3 {
        let (writes, failure) = (writes.clone(), failure.clone());
        net.attach(
            format!("sink{index}.pin"),
            DigitalAttachment::Sink(Rc::new(move |level| {
                if index == 1 {
                    return Err(failure.clone());
                }
                writes.borrow_mut().push((index, level));
                Ok(())
            })),
        )
        .unwrap();
    }
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    net.sample(0, &mut no_events).unwrap();
    let mut events = vec![];
    assert!(Arc::ptr_eq(
        &failure,
        &net.dispatch(0, 1, &mut |event| {
            events.push(event);
            Ok(())
        })
        .unwrap_err()
    ));
    assert_eq!(*writes.borrow(), [(0, true)]);
    assert_eq!(events.len(), 1);
    assert_eq!(net.observe().pending, 0);
    assert!(Arc::ptr_eq(
        &failure,
        &net.dispatch(0, 1, &mut no_events).unwrap_err()
    ));
    assert_eq!(*writes.borrow(), [(0, true)]);
}

#[test]
fn queue_and_endpoint_admission_are_bounded_before_any_sample() {
    for options in [
        DigitalNetOptions {
            max_pending: 0,
            ..Default::default()
        },
        DigitalNetOptions {
            dispatch_budget: 65537,
            ..Default::default()
        },
    ] {
        assert!(DigitalNet::new(options).is_err());
    }
    let (mut net, _, reads, _) = wired(DigitalNetOptions::default(), 1);
    assert!(net
        .attach(
            "late.pin".into(),
            DigitalAttachment::Sink(Rc::new(|_| Ok(())))
        )
        .is_err());
    assert_eq!(reads.get(), 0);
    net = DigitalNet::new(DigitalNetOptions::default()).unwrap();
    net.attach(
        "source.pin".into(),
        DigitalAttachment::Source(Rc::new(|| Ok(sample(None, 0, 0)))),
    )
    .unwrap();
    assert!(net
        .attach(
            "other.pin".into(),
            DigitalAttachment::Source(Rc::new(|| Ok(sample(None, 0, 0))))
        )
        .is_err());
    let sink = Rc::new(|_| Ok(())) as sim_host::fabric::digital::DigitalSink;
    net.attach("sink.pin".into(), DigitalAttachment::Sink(sink.clone()))
        .unwrap();
    assert!(net
        .attach("duplicate.pin".into(), DigitalAttachment::Sink(sink))
        .is_err());
    for index in 1..255 {
        net.attach(
            format!("sink{index}.pin"),
            DigitalAttachment::Sink(Rc::new(|_| Ok(()))),
        )
        .unwrap();
    }
    assert!(net
        .attach(
            "extra.pin".into(),
            DigitalAttachment::Sink(Rc::new(|_| Ok(())))
        )
        .unwrap_err()
        .to_string()
        .contains("capacity"));
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum Action {
    Step(String, u64, Option<bool>),
    Sample(String, u64, Option<bool>),
    Receive(String, u64, bool),
}
struct FixtureState {
    completed: DigitalSample,
    received: Option<bool>,
}
struct Fixture {
    name: String,
    level: bool,
    state: Rc<RefCell<FixtureState>>,
    actions: Rc<RefCell<Vec<Action>>>,
}
impl Stepper<HostInput, HostEvent> for Fixture {
    fn step(&mut self, step: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        let mut state = self.state.borrow_mut();
        self.actions.borrow_mut().push(Action::Step(
            self.name.clone(),
            step.t_sim_ns,
            state.received,
        ));
        state.completed = sample(Some(self.level), 500 + step.t_sim_ns, step.t_sim_ns);
        Ok(StepResult::reached(step.t_sim_ns))
    }
}
impl Component<HostInput, HostEvent> for Fixture {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        Some(self)
    }
}
struct FixtureFactory {
    actions: Rc<RefCell<Vec<Action>>>,
}
impl Factory<HostOptions, HostInput, HostEvent> for FixtureFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut ports = vec![];
        for (name, direction) in [("out", Direction::Out), ("in", Direction::In)] {
            let mut port = PortSpec::new(name, "digital");
            port.direction = direction;
            port.category = PortCategory::Signal;
            ports.push(port);
        }
        Ok(NodeDescription::new(ports, [Capability::Step]))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let state = Rc::new(RefCell::new(FixtureState {
            completed: sample(None, 500, 0),
            received: None,
        }));
        let (source_state, source_actions, source_name) =
            (state.clone(), self.actions.clone(), node.name.clone());
        build.attach::<_, ()>(
            "out",
            DigitalAttachment::Source(Rc::new(move || {
                let completed = source_state.borrow().completed;
                source_actions.borrow_mut().push(Action::Sample(
                    source_name.clone(),
                    completed.mapped_time_ns,
                    completed.level,
                ));
                Ok(completed)
            })),
        )?;
        let (sink_state, sink_actions, sink_name, context) = (
            state.clone(),
            self.actions.clone(),
            node.name.clone(),
            build.context(),
        );
        build.attach::<_, ()>(
            "in",
            DigitalAttachment::Sink(Rc::new(move |level| {
                sink_actions.borrow_mut().push(Action::Receive(
                    sink_name.clone(),
                    context.t_sim_ns()?,
                    level,
                ));
                sink_state.borrow_mut().received = Some(level);
                Ok(())
            })),
        )?;
        Ok(Box::new(Fixture {
            name: node.name.clone(),
            level: node.name == "right",
            state,
            actions: self.actions.clone(),
        }))
    }
}
fn link(source: &str, target: &str) -> LinkSpec {
    LinkSpec::new(
        Endpoint::parse(source).unwrap(),
        Endpoint::parse(target).unwrap(),
    )
}

#[test]
fn simulation_samples_every_source_before_sink_writes_and_next_interval_consumes_inputs() {
    let actions = Rc::new(RefCell::new(vec![]));
    let mut registry = registry().unwrap();
    registry
        .register(
            "fixture",
            FixtureFactory {
                actions: actions.clone(),
            },
        )
        .unwrap();
    let nodes = vec![
        NodeSpec::new("left", "fixture", HostOptions::CanHost),
        NodeSpec::new("right", "fixture", HostOptions::CanHost),
        NodeSpec::new(
            "left_wire",
            "net.digital",
            HostOptions::DigitalNet(Default::default()),
        ),
        NodeSpec::new(
            "right_wire",
            "net.digital",
            HostOptions::DigitalNet(Default::default()),
        ),
    ];
    let topology = Topology::new(
        nodes,
        vec![
            link("left.out", "left_wire.signal"),
            link("left_wire.signal", "right.in"),
            link("right.out", "right_wire.signal"),
            link("right_wire.signal", "left.in"),
        ],
    )
    .unwrap();
    let mut simulation = HostSimulation::new(
        topology,
        registry,
        SimulationOptions {
            quantum_ns: 10,
            ..Default::default()
        },
    )
    .unwrap();
    simulation.start(None).unwrap();
    assert_eq!(
        *actions.borrow(),
        [
            Action::Sample("left".into(), 0, None),
            Action::Sample("right".into(), 0, None)
        ]
    );
    assert!(!simulation
        .events()
        .iter()
        .any(|event| event.kind == "net.digital.delivery"));
    actions.borrow_mut().clear();
    simulation.step(Some(10), None).unwrap();
    assert_eq!(
        *actions.borrow(),
        [
            Action::Step("left".into(), 10, None),
            Action::Step("right".into(), 10, None),
            Action::Sample("left".into(), 10, Some(false)),
            Action::Sample("right".into(), 10, Some(true)),
            Action::Receive("right".into(), 10, false),
            Action::Receive("left".into(), 10, true),
        ]
    );
    let deliveries: Vec<_> = simulation
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::DigitalDelivery {
                source,
                destination,
                level,
                sequence,
                due_ns,
            }) => Some((
                source,
                destination,
                level,
                sequence,
                due_ns,
                event.source_time_ns,
                event.mapped_time_ns,
                event.delivery_time_ns,
                event.quality,
            )),
            _ => None,
        })
        .collect();
    assert_eq!(
        deliveries,
        [
            (
                "left.out".into(),
                "right.in".into(),
                false,
                0,
                10,
                Some(510),
                10,
                10,
                TimeQuality::Sampled
            ),
            (
                "right.out".into(),
                "left.in".into(),
                true,
                0,
                10,
                Some(510),
                10,
                10,
                TimeQuality::Sampled
            ),
        ]
    );
    actions.borrow_mut().clear();
    simulation.run_until(10, None).unwrap();
    assert!(actions.borrow().is_empty());
    simulation.step(Some(10), None).unwrap();
    assert_eq!(
        *actions.borrow(),
        [
            Action::Step("left".into(), 20, Some(true)),
            Action::Step("right".into(), 20, Some(false)),
            Action::Sample("left".into(), 20, Some(false)),
            Action::Sample("right".into(), 20, Some(true)),
        ]
    );
    let fixture = simulation.handle::<Fixture>("left").unwrap();
    simulation
        .with_component(&fixture, |fixture, _| {
            fixture.state.borrow_mut().completed = sample(None, 521, 21);
            Ok(())
        })
        .unwrap();
    let network = simulation.handle::<DigitalNetwork>("left_wire").unwrap();
    let failure = simulation
        .with_component(&network, |network, context| network.sample(21, context))
        .unwrap_err();
    assert!(failure.to_string().contains("lost output drive"));
    assert_eq!(simulation.state(), SimulationState::Failed);
    assert!(Arc::ptr_eq(&failure, &simulation.failure().unwrap()));
    assert_eq!(simulation.t_sim_ns(), 20);
    simulation.close().unwrap();
}

#[test]
fn pure_topology_admission_rejects_ambiguous_and_missing_drivers_before_resources() {
    struct EndpointFactory(Direction);
    impl Factory<HostOptions, HostInput, HostEvent> for EndpointFactory {
        fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
            let mut port = PortSpec::new("pin", "digital");
            port.direction = self.0;
            port.category = PortCategory::Signal;
            Ok(NodeDescription::new(vec![port], []))
        }
        fn create(
            &self,
            _: &NodeSpec<HostOptions>,
            _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
        ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
            panic!("pure admission must not create resources")
        }
    }
    for roles in [
        [Direction::Out, Direction::Out],
        [Direction::In, Direction::In],
        [Direction::Out, Direction::InOut],
    ] {
        let mut registry = registry().unwrap();
        registry
            .register("first", EndpointFactory(roles[0]))
            .unwrap();
        registry
            .register("second", EndpointFactory(roles[1]))
            .unwrap();
        let topology = Topology::new(
            vec![
                NodeSpec::new(
                    "wire",
                    "net.digital",
                    HostOptions::DigitalNet(Default::default()),
                ),
                NodeSpec::new("first", "first", HostOptions::CanHost),
                NodeSpec::new("second", "second", HostOptions::CanHost),
            ],
            vec![
                link("wire.signal", "first.pin"),
                link("second.pin", "wire.signal"),
            ],
        )
        .unwrap();
        assert!(HostSimulation::new(topology, registry, Default::default()).is_err());
    }
}
