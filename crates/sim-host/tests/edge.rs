#![cfg(feature = "simulation")]
use sim_core::{Failure, Result, Session};
use sim_host::fabric::edge::{
    DigitalEdge, DigitalEdgeNet, EdgeAttachment, EdgeDelivery, EdgeNetOptions, EdgeSender,
};
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
};
fn edge(level: bool, source: u64, mapped: u64) -> DigitalEdge {
    DigitalEdge {
        level,
        source_time_ns: source,
        mapped_time_ns: mapped,
    }
}
fn no_events(_: EdgeDelivery) -> Result<()> {
    Ok(())
}
fn net(options: EdgeNetOptions) -> (DigitalEdgeNet, EdgeSender) {
    let mut net = DigitalEdgeNet::new(options).unwrap();
    let sender = net
        .attach("source.out".into(), EdgeAttachment::Source)
        .unwrap()
        .unwrap();
    (net, sender)
}
#[test]
fn fifo_preserves_equal_times_repeated_levels_and_dispatch_floor() {
    let (mut net, sender) = net(EdgeNetOptions {
        max_pending: 4,
        dispatch_budget: 2,
    });
    let received = Rc::new(RefCell::new(vec![]));
    let target = received.clone();
    net.attach(
        "sink.input".into(),
        EdgeAttachment::Sink(Rc::new(move |edge| {
            target.borrow_mut().push(edge);
            Ok(())
        })),
    )
    .unwrap();
    net.reset(Session::new(1, 10).unwrap()).unwrap();
    let edges = [
        edge(false, 100, 3),
        edge(false, 100, 3),
        edge(true, 101, 10),
        edge(false, 102, 20),
    ];
    for item in edges {
        sender.send(item, 12).unwrap();
    }
    assert_eq!(net.next_due_ns().unwrap(), Some(12));
    assert_eq!(net.dispatch(11, 99, &mut no_events).unwrap(), 0);
    let mut events = vec![];
    assert_eq!(
        net.dispatch(12, 99, &mut |event| {
            events.push(event);
            Ok(())
        })
        .unwrap(),
        2
    );
    assert_eq!(*received.borrow(), edges[..2]);
    assert_eq!(
        events
            .iter()
            .map(|e| (e.sequence, e.due_ns, e.cut_ns))
            .collect::<Vec<_>>(),
        [(0, 12, 12), (1, 12, 12)]
    );
    assert_eq!(net.dispatch(12, 99, &mut no_events).unwrap(), 1);
    assert_eq!(net.next_due_ns().unwrap(), Some(20));
    assert_eq!(net.dispatch(20, 99, &mut no_events).unwrap(), 1);
    assert_eq!(*received.borrow(), edges);
    net.close().unwrap();
    assert!(sender.send(edge(false, 103, 21), 21).is_err());
}
#[test]
fn source_regression_and_capacity_preserve_first_failure_before_enqueue() {
    for invalid in [edge(false, 99, 2), edge(false, 101, 0), edge(false, 101, 2)] {
        let (mut net, sender) = net(EdgeNetOptions {
            max_pending: 1,
            ..Default::default()
        });
        net.attach(
            "sink.input".into(),
            EdgeAttachment::Sink(Rc::new(|_| panic!("failed queue cannot dispatch"))),
        )
        .unwrap();
        net.reset(Session::new(1, 0).unwrap()).unwrap();
        sender.send(edge(true, 100, 1), 0).unwrap();
        let failure = sender.send(invalid, 0).unwrap_err();
        assert_eq!(net.pending(), 1);
        assert!(Arc::ptr_eq(
            &failure,
            &net.dispatch(5, 5, &mut no_events).unwrap_err()
        ));
        assert!(Arc::ptr_eq(
            &failure,
            &sender.send(edge(false, 101, 2), 0).unwrap_err()
        ));
        net.reset(Session::new(2, 10).unwrap()).unwrap();
        assert_eq!(net.pending(), 0);
        assert!(net.failure().is_none());
    }
}
#[test]
fn partial_sink_failure_removes_broadcast_and_stops_later_sinks() {
    let (mut net, sender) = net(Default::default());
    let writes = Rc::new(RefCell::new(vec![]));
    let failure: Failure = Arc::new(std::io::Error::other("sink failed"));
    for index in 0..3 {
        let (writes, failure) = (writes.clone(), failure.clone());
        net.attach(
            format!("sink{index}.input"),
            EdgeAttachment::Sink(Rc::new(move |_| {
                if index == 1 {
                    return Err(failure.clone());
                }
                writes.borrow_mut().push(index);
                Ok(())
            })),
        )
        .unwrap();
    }
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    sender.send(edge(true, 0, 0), 0).unwrap();
    sender.send(edge(false, 1, 1), 0).unwrap();
    let mut observed = vec![];
    assert!(Arc::ptr_eq(
        &failure,
        &net.dispatch(2, 2, &mut |e| {
            observed.push(e);
            Ok(())
        })
        .unwrap_err()
    ));
    assert_eq!(*writes.borrow(), [0]);
    assert_eq!(observed.len(), 1);
    assert_eq!(net.pending(), 1);
}
#[test]
fn caught_feedback_overflow_stops_fanout_and_feedback_dispatch_is_bounded() {
    let (mut net, sender) = net(EdgeNetOptions {
        max_pending: 1,
        dispatch_budget: 3,
    });
    let feedback = sender.clone();
    let calls = Rc::new(Cell::new(0));
    let count = calls.clone();
    let caught = Rc::new(RefCell::new(None));
    let saved = caught.clone();
    net.attach(
        "sink0.input".into(),
        EdgeAttachment::Sink(Rc::new(move |e| {
            count.set(count.get() + 1);
            feedback.send(e, 0)?;
            if count.get() == 4 {
                *saved.borrow_mut() = Some(feedback.send(e, 0).unwrap_err());
            }
            Ok(())
        })),
    )
    .unwrap();
    let later = Rc::new(Cell::new(0));
    let later_calls = later.clone();
    net.attach(
        "sink1.input".into(),
        EdgeAttachment::Sink(Rc::new(move |_| {
            later_calls.set(later_calls.get() + 1);
            Ok(())
        })),
    )
    .unwrap();
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    sender.send(edge(true, 0, 0), 0).unwrap();
    assert_eq!(net.dispatch(0, 99, &mut no_events).unwrap(), 3);
    assert_eq!((calls.get(), later.get(), net.pending()), (3, 3, 1));
    let failure = net.dispatch(0, 99, &mut no_events).unwrap_err();
    assert!(Arc::ptr_eq(&failure, caught.borrow().as_ref().unwrap()));
    assert_eq!((calls.get(), later.get(), net.pending()), (4, 3, 1));
}
#[test]
fn wiring_rejects_duplicate_endpoints_sources_callbacks_and_capacity() {
    assert!(DigitalEdgeNet::new(EdgeNetOptions {
        max_pending: 0,
        ..Default::default()
    })
    .is_err());
    let (mut net, _) = net(Default::default());
    assert!(net
        .attach("other.out".into(), EdgeAttachment::Source)
        .is_err());
    let sink: sim_host::fabric::edge::EdgeSink = Rc::new(|_| Ok(()));
    net.attach("sink.input".into(), EdgeAttachment::Sink(sink.clone()))
        .unwrap();
    assert!(net
        .attach("same.input".into(), EdgeAttachment::Sink(sink))
        .is_err());
    for index in 1..255 {
        net.attach(
            format!("sink{index}.input"),
            EdgeAttachment::Sink(Rc::new(|_| Ok(()))),
        )
        .unwrap();
    }
    assert!(net
        .attach(
            "extra.input".into(),
            EdgeAttachment::Sink(Rc::new(|_| Ok(())))
        )
        .err()
        .unwrap()
        .to_string()
        .contains("capacity"));
}

use sim_core::{
    BuildContext, Capability, Component, Direction, Endpoint, EventPayload, Factory, LinkSpec,
    NodeDescription, NodeSpec, PortCategory, PortSpec, SimulationOptions, SimulationState,
    StepContext, StepResult, Stepper, TimeQuality, Topology,
};
use sim_host::{
    fabric::edge::EdgeOutput,
    runtime::{registry, HostContext, HostEvent, HostInput, HostOptions, HostSimulation},
};
struct Source {
    output: EdgeOutput,
    script: Vec<DigitalEdge>,
    sent: bool,
    future: bool,
}
impl Source {
    fn send(&mut self) -> Result<()> {
        if !self.sent {
            for edge in &self.script {
                self.output.send(*edge)?;
            }
            self.sent = true;
        }
        Ok(())
    }
}
impl Stepper<HostInput, HostEvent> for Source {
    fn step(&mut self, step: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        self.send()?;
        Ok(StepResult::reached(step.t_sim_ns))
    }
}
impl Component<HostInput, HostEvent> for Source {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
    fn start(&mut self, _: &HostContext) -> Result<()> {
        if self.future {
            self.send()?;
        }
        Ok(())
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        Some(self)
    }
}
struct SourceFactory {
    script: Vec<DigitalEdge>,
    future: bool,
    saved: Rc<RefCell<Option<EdgeOutput>>>,
}
impl Factory<HostOptions, HostInput, HostEvent> for SourceFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("out", "digital-edge");
        port.direction = Direction::Out;
        port.category = PortCategory::Signal;
        port.capabilities.insert("event-output".into());
        Ok(NodeDescription::new(vec![port], [Capability::Step]))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let output = build.attach::<_, EdgeOutput>("out", EdgeAttachment::Source)?;
        *self.saved.borrow_mut() = Some(output.clone());
        Ok(Box::new(Source {
            output,
            script: self.script.clone(),
            sent: false,
            future: self.future,
        }))
    }
}
struct Sink;
impl Component<HostInput, HostEvent> for Sink {
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
}
type Received = Rc<RefCell<Vec<(DigitalEdge, u64)>>>;
struct SinkFactory {
    received: Received,
    failure: Option<Failure>,
}
impl Factory<HostOptions, HostInput, HostEvent> for SinkFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("input", "digital-edge");
        port.direction = Direction::In;
        port.category = PortCategory::Signal;
        Ok(NodeDescription::new(vec![port], []))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let (received, failure, context) =
            (self.received.clone(), self.failure.clone(), build.context());
        build.attach::<_, ()>(
            "input",
            EdgeAttachment::Sink(Rc::new(move |edge| {
                received.borrow_mut().push((edge, context.t_sim_ns()?));
                if let Some(failure) = &failure {
                    let _caught = context.fail(failure.clone());
                    return Err(Arc::new(std::io::Error::other("later sink failure")));
                }
                Ok(())
            })),
        )?;
        Ok(Box::new(Sink))
    }
}
fn link(source: &str, target: &str) -> LinkSpec {
    LinkSpec::new(
        Endpoint::parse(source).unwrap(),
        Endpoint::parse(target).unwrap(),
    )
}
fn simulation(
    script: Vec<DigitalEdge>,
    future: bool,
    failure: Option<Failure>,
) -> (HostSimulation, Received, Rc<RefCell<Option<EdgeOutput>>>) {
    let (received, saved) = (Rc::new(RefCell::new(vec![])), Rc::new(RefCell::new(None)));
    let mut registry = registry().unwrap();
    registry
        .register(
            "source",
            SourceFactory {
                script,
                future,
                saved: saved.clone(),
            },
        )
        .unwrap();
    registry
        .register(
            "sink",
            SinkFactory {
                received: received.clone(),
                failure,
            },
        )
        .unwrap();
    let topology = Topology::new(
        vec![
            NodeSpec::new("source", "source", HostOptions::CanHost),
            NodeSpec::new(
                "wire",
                "net.digital-edge",
                HostOptions::DigitalEdgeNet(Default::default()),
            ),
            NodeSpec::new("sink", "sink", HostOptions::CanHost),
        ],
        vec![
            link("source.out", "wire.signal"),
            link("wire.signal", "sink.input"),
        ],
    )
    .unwrap();
    (
        HostSimulation::new(
            topology,
            registry,
            SimulationOptions {
                quantum_ns: 1_000_000,
                ..Default::default()
            },
        )
        .unwrap(),
        received,
        saved,
    )
}
#[test]
fn simulation_delivers_every_explicit_edge_at_current_cut_with_exact_metadata() {
    // Canonical examples/edge-events uses this exact within-interval script.
    let script = vec![
        edge(true, 5100, 100),
        edge(false, 5101, 101),
        edge(true, 5101, 101),
        edge(false, 5999, 999),
    ];
    let (mut sim, received, saved) = simulation(script.clone(), false, None);
    sim.start(None).unwrap();
    sim.step(None, None).unwrap();
    assert_eq!(
        *received.borrow(),
        script.iter().map(|e| (*e, 1_000_000)).collect::<Vec<_>>()
    );
    let events: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| {
            if let EventPayload::Observation(HostEvent::DigitalEdgeDelivery {
                sequence,
                due_ns,
                ..
            }) = event.payload
            {
                Some((
                    sequence,
                    due_ns,
                    event.source_time_ns,
                    event.mapped_time_ns,
                    event.delivery_time_ns,
                    event.quality,
                ))
            } else {
                None
            }
        })
        .collect();
    assert_eq!(
        events,
        script
            .iter()
            .enumerate()
            .map(|(i, e)| (
                i as u64,
                1_000_000,
                Some(e.source_time_ns),
                e.mapped_time_ns,
                1_000_000,
                TimeQuality::Exact
            ))
            .collect::<Vec<_>>()
    );
    let stale = saved.borrow().as_ref().unwrap().clone();
    sim.reset(None).unwrap();
    assert!(stale.send(edge(true, 6000, 1000)).is_err());
    assert_ne!(sim.state(), SimulationState::Failed);
    sim.close().unwrap();
}
#[test]
fn known_future_edges_split_actual_simulation_cuts() {
    let script = vec![edge(true, 103, 3), edge(true, 104, 4), edge(false, 110, 10)];
    let (mut sim, received, _) = simulation(script.clone(), true, None);
    sim.start(None).unwrap();
    sim.run_until(10, None).unwrap();
    assert_eq!(
        *received.borrow(),
        script
            .iter()
            .map(|e| (*e, e.mapped_time_ns))
            .collect::<Vec<_>>()
    );
    assert_eq!(sim.t_sim_ns(), 10);
    sim.close().unwrap();
}
#[test]
fn caught_global_failure_wins_over_later_callback_error_and_freezes_delivery() {
    let failure: Failure = Arc::new(std::io::Error::other("first terminal error"));
    let (mut sim, received, saved) = simulation(
        vec![edge(true, 1, 1), edge(false, 2, 2)],
        false,
        Some(failure.clone()),
    );
    sim.start(None).unwrap();
    let actual = sim.step(None, None).unwrap_err();
    assert!(Arc::ptr_eq(&failure, &actual));
    assert!(Arc::ptr_eq(&failure, &sim.failure().unwrap()));
    assert_eq!(sim.state(), SimulationState::Failed);
    assert_eq!(received.borrow().len(), 1);
    assert!(sim
        .events()
        .iter()
        .all(|event| event.kind != "net.digital-edge.delivery"));
    assert!(Arc::ptr_eq(
        &failure,
        &saved
            .borrow()
            .as_ref()
            .unwrap()
            .send(edge(true, 3, 3))
            .unwrap_err()
    ));
    sim.close().unwrap();
}
#[test]
fn topology_rejects_missing_event_output_and_bad_roles_before_factories_run() {
    struct Invalid {
        direction: Direction,
        capability: bool,
    }
    impl Factory<HostOptions, HostInput, HostEvent> for Invalid {
        fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
            let mut port = PortSpec::new("pin", "digital-edge");
            port.direction = self.direction;
            port.category = PortCategory::Signal;
            if self.capability {
                port.capabilities.insert("event-output".into());
            }
            Ok(NodeDescription::new(vec![port], []))
        }
        fn create(
            &self,
            _: &NodeSpec<HostOptions>,
            _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
        ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
            panic!("admission must precede resource acquisition")
        }
    }
    for (first, second, capability) in [
        (Direction::Out, Direction::In, false),
        (Direction::Out, Direction::Out, true),
        (Direction::In, Direction::In, true),
        (Direction::Out, Direction::InOut, true),
    ] {
        let mut registry = registry().unwrap();
        registry
            .register(
                "first",
                Invalid {
                    direction: first,
                    capability,
                },
            )
            .unwrap();
        registry
            .register(
                "second",
                Invalid {
                    direction: second,
                    capability,
                },
            )
            .unwrap();
        let topology = Topology::new(
            vec![
                NodeSpec::new("first", "first", HostOptions::CanHost),
                NodeSpec::new("second", "second", HostOptions::CanHost),
                NodeSpec::new(
                    "wire",
                    "net.digital-edge",
                    HostOptions::DigitalEdgeNet(Default::default()),
                ),
            ],
            vec![
                link("first.pin", "wire.signal"),
                link("wire.signal", "second.pin"),
            ],
        )
        .unwrap();
        assert!(HostSimulation::new(topology, registry, Default::default()).is_err());
    }
}
