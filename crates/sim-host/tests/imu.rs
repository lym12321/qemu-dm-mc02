#![cfg(feature = "simulation")]

use sim_core::{
    BuildContext, Capability, Component, Direction, Endpoint, EventPayload, Factory, LinkSpec,
    NodeDescription, NodeSpec, PortCategory, PortSpec, Result, Session, SimulationOptions,
    StepContext, StepResult, Stepper, TimeQuality, Topology,
};
use sim_host::{
    fabric::imu::{ImuAttachment, ImuNet, ImuNetOptions, ImuObservation},
    runtime::{registry, HostContext, HostEvent, HostInput, HostOptions, HostSimulation},
    signals::{ImuSample, ImuSignalSample},
};
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
};

fn sample(source_time_ns: Option<u64>, mapped_time_ns: u64) -> ImuSignalSample {
    ImuSignalSample {
        sample: ImuSample {
            gyro_rad_s: [-0.5, 0.0, 1.25],
            accel_m_s2: [0.0, -1.0, 9.80665],
        },
        source_time_ns,
        mapped_time_ns,
    }
}
fn no_events(_: ImuObservation) -> Result<()> {
    Ok(())
}
type Writes = Rc<RefCell<Vec<(usize, ImuSignalSample)>>>;
fn wired(options: ImuNetOptions, count: usize) -> (ImuNet, Rc<Cell<ImuSignalSample>>, Writes) {
    let mut net = ImuNet::new(options).unwrap();
    let value = Rc::new(Cell::new(sample(Some(500), 0)));
    let source = value.clone();
    net.attach(
        "world.imu".into(),
        ImuAttachment::Source(Rc::new(move || Ok(source.get()))),
    )
    .unwrap();
    let writes = Rc::new(RefCell::new(vec![]));
    for i in 0..count {
        let writes = writes.clone();
        net.attach(
            format!("sink{i}.imu"),
            ImuAttachment::Sink(Rc::new(move |value| {
                writes.borrow_mut().push((i, value));
                Ok(())
            })),
        )
        .unwrap();
    }
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    (net, value, writes)
}

#[test]
fn every_completed_cut_including_unchanged_measurements_fans_out_the_full_si_envelope() {
    let (mut net, value, writes) = wired(ImuNetOptions::default(), 2);
    for cut in [0, 10] {
        value.set(sample(Some(500), cut));
        let mut events = vec![];
        net.sample(cut, &mut |event| {
            events.push(event);
            Ok(())
        })
        .unwrap();
        assert_eq!(
            events,
            [ImuObservation::Sample {
                source: "world.imu".into(),
                sample: sample(Some(500), cut),
                cut_ns: cut
            }]
        );
        assert_eq!(writes.borrow().len(), cut as usize / 10 * 2);
        assert_eq!(net.dispatch(cut, 1, &mut no_events).unwrap(), 1);
    }
    assert_eq!(
        *writes.borrow(),
        [
            (0, sample(Some(500), 0)),
            (1, sample(Some(500), 0)),
            (0, sample(Some(500), 10)),
            (1, sample(Some(500), 10))
        ]
    );
    assert!(net
        .sample(10, &mut no_events)
        .unwrap_err()
        .to_string()
        .contains("once per completed cut"));
}

#[test]
fn finite_si_exact_completed_cut_and_last_known_source_time_are_admitted_before_publication() {
    let mut invalid = sample(Some(510), 20);
    invalid.sample.accel_m_s2[2] = f64::INFINITY;
    for bad in [
        invalid,
        sample(Some(510), 19),
        sample(Some(510), 21),
        sample(Some(499), 20),
    ] {
        let (mut net, value, writes) = wired(ImuNetOptions::default(), 1);
        net.sample(0, &mut no_events).unwrap();
        net.dispatch(0, 1, &mut no_events).unwrap();
        // Absent native time cannot erase the previous known timestamp.
        value.set(sample(None, 10));
        net.sample(10, &mut no_events).unwrap();
        net.dispatch(10, 1, &mut no_events).unwrap();
        value.set(bad);
        let failure = net.sample(20, &mut no_events).unwrap_err();
        assert_eq!(net.observe().sample, Some(sample(None, 10)));
        assert_eq!(writes.borrow().len(), 2);
        assert!(Arc::ptr_eq(
            &failure,
            &net.dispatch(20, 1, &mut no_events).unwrap_err()
        ));
        net.reset(Session::new(2, 30).unwrap()).unwrap();
        value.set(sample(Some(1), 30));
        net.sample(30, &mut no_events).unwrap();
    }
}

#[test]
fn overflow_preserves_committed_envelope() {
    let (mut net, value, _) = wired(
        ImuNetOptions {
            max_pending: 1,
            ..Default::default()
        },
        1,
    );
    net.sample(0, &mut no_events).unwrap();
    value.set(sample(Some(510), 10));
    assert!(net
        .sample(10, &mut no_events)
        .unwrap_err()
        .to_string()
        .contains("capacity"));
    assert_eq!(net.observe().sample, Some(sample(Some(500), 0)));
}

#[derive(Clone, Debug, PartialEq)]
enum Action {
    Read(u64),
    Receive(u64),
    Step(String, u64, Option<u64>),
}
struct Fixture {
    name: String,
    completed: Rc<Cell<ImuSignalSample>>,
    received: Rc<Cell<Option<ImuSignalSample>>>,
    actions: Rc<RefCell<Vec<Action>>>,
}
impl Stepper<HostInput, HostEvent> for Fixture {
    fn step(&mut self, step: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        self.actions.borrow_mut().push(Action::Step(
            self.name.clone(),
            step.t_sim_ns,
            self.received.get().map(|value| value.mapped_time_ns),
        ));
        if self.name == "world" {
            self.completed.set(sample(None, step.t_sim_ns));
        }
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
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("imu", "imu");
        port.category = PortCategory::Signal;
        port.direction = if node.name == "world" {
            Direction::Out
        } else {
            Direction::In
        };
        Ok(NodeDescription::new(vec![port], [Capability::Step]))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let completed = Rc::new(Cell::new(sample(None, 0)));
        let received = Rc::new(Cell::new(None));
        let actions = self.actions.clone();
        if node.name == "world" {
            let completed = completed.clone();
            build.attach::<_, ()>(
                "imu",
                ImuAttachment::Source(Rc::new(move || {
                    let sample = completed.get();
                    actions
                        .borrow_mut()
                        .push(Action::Read(sample.mapped_time_ns));
                    Ok(sample)
                })),
            )?;
        } else {
            let received = received.clone();
            build.attach::<_, ()>(
                "imu",
                ImuAttachment::Sink(Rc::new(move |sample| {
                    actions
                        .borrow_mut()
                        .push(Action::Receive(sample.mapped_time_ns));
                    received.set(Some(sample));
                    Ok(())
                })),
            )?;
        }
        Ok(Box::new(Fixture {
            name: node.name.clone(),
            completed,
            received,
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
fn simulation_samples_completed_state_then_delivers_for_the_next_interval() {
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
    let topology = Topology::new(
        vec![
            NodeSpec::new("world", "fixture", HostOptions::CanHost),
            NodeSpec::new("sink", "fixture", HostOptions::CanHost),
            NodeSpec::new("wire", "net.imu", HostOptions::ImuNet(Default::default())),
        ],
        vec![
            link("world.imu", "wire.signal"),
            link("wire.signal", "sink.imu"),
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
    assert_eq!(*actions.borrow(), [Action::Read(0), Action::Receive(0)]);
    actions.borrow_mut().clear();
    simulation.step(Some(10), None).unwrap();
    assert_eq!(
        *actions.borrow(),
        [
            Action::Step("world".into(), 10, None),
            Action::Step("sink".into(), 10, Some(0)),
            Action::Read(10),
            Action::Receive(10)
        ]
    );
    actions.borrow_mut().clear();
    simulation.step(Some(10), None).unwrap();
    assert_eq!(
        *actions.borrow(),
        [
            Action::Step("world".into(), 20, None),
            Action::Step("sink".into(), 20, Some(10)),
            Action::Read(20),
            Action::Receive(20)
        ]
    );
    let delivered: Vec<_> = simulation
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::ImuDelivery {
                source,
                destination,
                sample,
                sequence,
                due_ns,
            }) => Some((
                source,
                destination,
                sample,
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
    let expected: Vec<_> = [0, 10, 20]
        .into_iter()
        .enumerate()
        .map(|(index, cut)| {
            (
                "world.imu".into(),
                "sink.imu".into(),
                sample(None, cut),
                index as u64,
                cut,
                None,
                cut,
                cut,
                TimeQuality::Sampled,
            )
        })
        .collect();
    assert_eq!(delivered, expected);
    simulation.close().unwrap();
}
