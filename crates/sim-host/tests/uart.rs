#![cfg(feature = "simulation")]

use sim_core::{
    BuildContext, Component, Direction, Endpoint, EventPayload, Factory, Failure, LinkSpec,
    NodeDescription, NodeSpec, PortSpec, Result, Session, SimulationOptions, SimulationState,
    TimeQuality, Topology,
};
use sim_host::{
    fabric::uart::{
        UartAttachment, UartConnection, UartEndpointId, UartNet, UartNetOptions, UartObservation,
        UartStamp,
    },
    runtime::{registry, HostContext, HostEvent, HostInput, HostOptions, HostSimulation},
};
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
};

fn stamp(source: Option<u64>, mapped: Option<u64>, quality: TimeQuality) -> UartStamp {
    UartStamp {
        source_time_ns: source,
        mapped_time_ns: mapped,
        quality,
    }
}
type Writes = Rc<RefCell<Vec<Vec<u8>>>>;
type Observations = Rc<RefCell<Vec<UartObservation>>>;
struct Wired {
    net: UartNet,
    left: UartEndpointId,
    right: UartEndpointId,
    left_writes: Writes,
    right_writes: Writes,
    events: Observations,
}
fn wired(options: UartNetOptions) -> Wired {
    let events = Rc::new(RefCell::new(vec![]));
    let observed = events.clone();
    let mut net = UartNet::new(
        options,
        Rc::new(move |event| {
            observed.borrow_mut().push(event);
            Ok(())
        }),
    )
    .unwrap();
    let (left, right) = (UartEndpointId::new(), UartEndpointId::new());
    let (left_writes, right_writes) =
        (Rc::new(RefCell::new(vec![])), Rc::new(RefCell::new(vec![])));
    for (name, endpoint, writes) in [
        ("left.uart", left.clone(), left_writes.clone()),
        ("right.uart", right.clone(), right_writes.clone()),
    ] {
        net.attach(
            name.into(),
            UartAttachment {
                endpoint,
                receive: Rc::new(move |data, _| {
                    writes.borrow_mut().push(data.to_vec());
                    Ok(())
                }),
            },
        )
        .unwrap();
    }
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    Wired {
        net,
        left,
        right,
        left_writes,
        right_writes,
        events,
    }
}

#[test]
fn opaque_binary_chunks_keep_source_time_floor_quality_and_stable_ties() {
    let mut fixture = wired(UartNetOptions {
        delay_ns: 2,
        ..Default::default()
    });
    fixture
        .net
        .send(
            &fixture.left,
            &[0, 0x7f],
            stamp(Some(901), Some(3), TimeQuality::UpperBound),
            10,
            10,
        )
        .unwrap();
    fixture
        .net
        .send(
            &fixture.left,
            &[0x80, 0xff],
            stamp(Some(902), Some(4), TimeQuality::UpperBound),
            10,
            10,
        )
        .unwrap();
    fixture
        .net
        .send(&fixture.right, b"back", UartStamp::default(), 10, 10)
        .unwrap();
    assert_eq!(
        (
            fixture.net.next_due_ns().unwrap(),
            fixture.net.observe().pending_bytes
        ),
        (Some(12), 8)
    );
    assert_eq!(fixture.net.dispatch(11, 10).unwrap(), 0);
    assert_eq!(fixture.net.dispatch(12, 2).unwrap(), 2);
    assert_eq!(
        *fixture.right_writes.borrow(),
        [vec![0, 0x7f], vec![0x80, 0xff]]
    );
    assert!(fixture.left_writes.borrow().is_empty());
    assert_eq!(fixture.net.dispatch(12, 2).unwrap(), 1);
    assert_eq!(*fixture.left_writes.borrow(), [b"back".to_vec()]);
    assert_eq!(fixture.net.observe().pending_bytes, 0);
    let events: Vec<_> = fixture
        .events
        .borrow()
        .iter()
        .filter(|event| event.destination.is_some())
        .cloned()
        .collect();
    assert_eq!(
        events[0],
        UartObservation {
            source: "left.uart".into(),
            destination: Some("right.uart".into()),
            data: vec![0, 0x7f],
            sequence: 0,
            due_ns: 12,
            source_time_ns: Some(901),
            mapped_time_ns: 3,
            delivery_time_ns: 12,
            quality: TimeQuality::UpperBound,
        }
    );
}

#[test]
fn invalid_chunk_and_mapped_time_are_pure_rejections_but_bounds_freeze_until_reset() {
    for options in [
        UartNetOptions {
            max_pending: 1,
            ..Default::default()
        },
        UartNetOptions {
            max_pending_bytes: 3,
            ..Default::default()
        },
    ] {
        let mut fixture = wired(options);
        fixture
            .net
            .send(&fixture.left, b"", UartStamp::default(), 0, 0)
            .unwrap();
        assert!(fixture
            .net
            .send(&fixture.left, &[0; 4097], UartStamp::default(), 0, 0)
            .is_err());
        assert_eq!(
            (fixture.net.observe().pending, fixture.events.borrow().len()),
            (0, 0)
        );
        fixture
            .net
            .send(
                &fixture.left,
                b"ab",
                stamp(None, Some(10), TimeQuality::Sampled),
                0,
                0,
            )
            .unwrap();
        assert!(fixture
            .net
            .send(
                &fixture.left,
                b"older",
                stamp(None, Some(9), TimeQuality::Sampled),
                0,
                0
            )
            .unwrap_err()
            .to_string()
            .contains("decrease"));
        assert!(!fixture.net.observe().failed);
        let failure = fixture
            .net
            .send(&fixture.left, b"cd", UartStamp::default(), 10, 10)
            .unwrap_err();
        assert!(failure.to_string().contains("capacity"));
        assert_eq!(
            (
                fixture.net.observe().pending,
                fixture.net.observe().pending_bytes,
                fixture.events.borrow().len()
            ),
            (1, 2, 1)
        );
        assert!(Arc::ptr_eq(
            &failure,
            &fixture.net.next_due_ns().unwrap_err()
        ));
        assert!(Arc::ptr_eq(
            &failure,
            &fixture.net.dispatch(10, 10).unwrap_err()
        ));
        fixture.net.reset(Session::new(2, 0).unwrap()).unwrap();
        fixture
            .net
            .send(&fixture.left, b"z", UartStamp::default(), 0, 0)
            .unwrap();
        fixture.net.dispatch(0, 10).unwrap();
        assert_eq!(*fixture.right_writes.borrow(), [b"z".to_vec()]);
        assert_eq!(fixture.events.borrow().last().unwrap().sequence, 0);
    }
}

#[test]
fn default_byte_capacity_empty_noop_and_time_overflow_do_not_weaken_chunk_limit() {
    let mut fixture = wired(UartNetOptions::default());
    fixture
        .net
        .send(&fixture.left, b"", UartStamp::default(), 0, 0)
        .unwrap();
    for _ in 0..16 {
        fixture
            .net
            .send(&fixture.left, &[0xaa; 4096], UartStamp::default(), 0, 0)
            .unwrap();
    }
    assert_eq!(
        (
            fixture.net.observe().pending,
            fixture.net.observe().pending_bytes
        ),
        (16, 65536)
    );
    assert!(fixture
        .net
        .send(&fixture.left, b"!", UartStamp::default(), 0, 0)
        .is_err());
    fixture = wired(UartNetOptions {
        delay_ns: 1,
        ..Default::default()
    });
    assert!(fixture
        .net
        .send(
            &fixture.left,
            b"a",
            UartStamp::default(),
            u64::MAX,
            u64::MAX
        )
        .unwrap_err()
        .to_string()
        .contains("exceeds u64"));
    assert_eq!(
        (fixture.net.observe().pending, fixture.net.observe().failed),
        (0, false)
    );
}

#[test]
fn partial_sink_failure_is_not_replayed_and_borrowed_resources_are_not_closed() {
    let events = Rc::new(RefCell::new(vec![]));
    let observed = events.clone();
    let mut net = UartNet::new(
        Default::default(),
        Rc::new(move |event| {
            observed.borrow_mut().push(event);
            Ok(())
        }),
    )
    .unwrap();
    let source = UartEndpointId::new();
    net.attach(
        "source.uart".into(),
        UartAttachment {
            endpoint: source.clone(),
            receive: Rc::new(|_, _| Ok(())),
        },
    )
    .unwrap();
    let writes = Rc::new(RefCell::new(vec![]));
    let partial = writes.clone();
    let failure: Failure = Arc::new(std::io::Error::other("sink wrote prefix then failed"));
    let rejected = failure.clone();
    net.attach(
        "sink.uart".into(),
        UartAttachment {
            endpoint: UartEndpointId::new(),
            receive: Rc::new(move |data, _| {
                partial.borrow_mut().push(data[0]);
                Err(rejected.clone())
            }),
        },
    )
    .unwrap();
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    net.send(&source, b"abc", UartStamp::default(), 0, 0)
        .unwrap();
    assert!(Arc::ptr_eq(&failure, &net.dispatch(0, 10).unwrap_err()));
    assert_eq!(*writes.borrow(), [b'a']);
    assert_eq!(
        (
            net.observe().pending,
            net.observe().pending_bytes,
            events.borrow().len()
        ),
        (0, 0, 1)
    );
    assert!(Arc::ptr_eq(&failure, &net.dispatch(0, 10).unwrap_err()));
    net.close().unwrap();
    net.close().unwrap();
    assert_eq!(*writes.borrow(), [b'a']);
}

#[test]
fn caught_response_overflow_preserves_first_failure_over_replacement_and_stops_delivery() {
    for replacement in [false, true] {
        let mut net = UartNet::new(
            UartNetOptions {
                max_pending: 1,
                ..Default::default()
            },
            Rc::new(|_| Ok(())),
        )
        .unwrap();
        let (left, right) = (UartEndpointId::new(), UartEndpointId::new());
        net.attach(
            "left.uart".into(),
            UartAttachment {
                endpoint: left.clone(),
                receive: Rc::new(|_, _| panic!("queued reply must not deliver after failure")),
            },
        )
        .unwrap();
        let response = right.clone();
        let caught = Rc::new(RefCell::new(None));
        let rejected = caught.clone();
        net.attach(
            "right.uart".into(),
            UartAttachment {
                endpoint: right,
                receive: Rc::new(move |_, net| {
                    net.send(&response, b"accepted", UartStamp::default(), 0, 0)?;
                    *rejected.borrow_mut() = Some(
                        net.send(&response, b"rejected", UartStamp::default(), 0, 0)
                            .unwrap_err(),
                    );
                    if replacement {
                        return Err(Arc::new(std::io::Error::other("replacement error")));
                    }
                    Ok(())
                }),
            },
        )
        .unwrap();
        net.reset(Session::new(1, 0).unwrap()).unwrap();
        net.send(&left, b"first", UartStamp::default(), 0, 0)
            .unwrap();
        let failure = net.dispatch(0, 10).unwrap_err();
        assert!(Arc::ptr_eq(&failure, caught.borrow().as_ref().unwrap()));
        assert_eq!((net.observe().pending, net.observe().pending_bytes), (1, 8));
    }
}

struct Source {
    connection: UartConnection,
    closed: Rc<Cell<bool>>,
}
impl Component<HostInput, HostEvent> for Source {
    fn close(&mut self) -> Result<()> {
        self.closed.set(true);
        Ok(())
    }
    fn start(&mut self, _: &HostContext) -> Result<()> {
        self.connection.send(b"\0\xffbinary", UartStamp::default())
    }
}
struct SourceFactory {
    received: Writes,
    closed: Rc<Cell<bool>>,
    loopback: bool,
}
impl Factory<HostOptions, HostInput, HostEvent> for SourceFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("uart", "uart");
        port.min_connections = 1;
        Ok(NodeDescription::new(vec![port], []))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let endpoint = UartEndpointId::new();
        let (source, received, context, loopback) = (
            endpoint.clone(),
            self.received.clone(),
            build.context(),
            self.loopback,
        );
        let connection = build.attach(
            "uart",
            UartAttachment {
                endpoint,
                receive: Rc::new(move |data, net| {
                    received.borrow_mut().push(data.to_vec());
                    if loopback {
                        net.send(
                            &source,
                            data,
                            UartStamp::default(),
                            context.t_sim_ns()?,
                            context.not_before_ns()?,
                        )?;
                    }
                    Ok(())
                }),
            },
        )?;
        Ok(Box::new(Source {
            connection,
            closed: self.closed.clone(),
        }))
    }
}
fn link(source: &str, target: &str) -> LinkSpec {
    LinkSpec::new(
        Endpoint::parse(source).unwrap(),
        Endpoint::parse(target).unwrap(),
    )
}
fn echo_sim(loopback: bool, options: UartNetOptions) -> (HostSimulation, Writes, Rc<Cell<bool>>) {
    let (received, closed) = (Rc::new(RefCell::new(vec![])), Rc::new(Cell::new(false)));
    let mut registry = registry().unwrap();
    registry
        .register(
            "source",
            SourceFactory {
                received: received.clone(),
                closed: closed.clone(),
                loopback,
            },
        )
        .unwrap();
    let topology = Topology::new(
        vec![
            NodeSpec::new("wire", "net.uart", HostOptions::UartNet(options)),
            NodeSpec::new("source", "source", HostOptions::CanHost),
            NodeSpec::new("echo", "device.uart-echo", HostOptions::UartEcho),
        ],
        vec![
            link("source.uart", "wire.uart"),
            link("echo.uart", "wire.uart"),
        ],
    )
    .unwrap();
    (
        HostSimulation::new(topology, registry, SimulationOptions::default()).unwrap(),
        received,
        closed,
    )
}

#[test]
fn actual_echo_factory_replies_at_same_cut_and_reset_recreates_borrowed_capabilities() {
    let (mut sim, received, closed) = echo_sim(false, Default::default());
    sim.start(None).unwrap();
    assert_eq!(*received.borrow(), [b"\0\xffbinary".to_vec()]);
    let deliveries: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::UartDelivery {
                source,
                destination,
                data,
                sequence,
                due_ns,
            }) => Some((
                source,
                destination,
                data,
                sequence,
                due_ns,
                event.delivery_time_ns,
            )),
            _ => None,
        })
        .collect();
    assert_eq!(
        deliveries,
        [
            (
                "source.uart".into(),
                "echo.uart".into(),
                b"\0\xffbinary".to_vec(),
                0,
                0,
                0
            ),
            (
                "echo.uart".into(),
                "source.uart".into(),
                b"\0\xffbinary".to_vec(),
                1,
                0,
                0
            ),
        ]
    );
    let handle = sim.handle::<Source>("source").unwrap();
    let old = sim.component(&handle).unwrap().connection.clone();
    sim.reset(None).unwrap();
    assert!(closed.get());
    assert!(old.send(b"old", UartStamp::default()).is_err());
    assert_eq!(received.borrow().len(), 2);
    sim.close().unwrap();
}

#[test]
fn direct_paused_capacity_failure_freezes_simulation_and_generic_budget_bounds_echo_cycles() {
    let (mut sim, received, _) = echo_sim(
        false,
        UartNetOptions {
            max_pending: 1,
            ..Default::default()
        },
    );
    sim.start(None).unwrap();
    let source = sim.handle::<Source>("source").unwrap();
    let connection = sim.component(&source).unwrap().connection.clone();
    assert!(connection.send(&[0; 4097], UartStamp::default()).is_err());
    connection.send(b"", UartStamp::default()).unwrap();
    assert_eq!(sim.state(), SimulationState::Paused);
    connection.send(b"accepted", UartStamp::default()).unwrap();
    let failure = connection
        .send(b"rejected", UartStamp::default())
        .unwrap_err();
    assert!(Arc::ptr_eq(&failure, &sim.failure().unwrap()));
    assert_eq!(sim.state(), SimulationState::Failed);
    assert_eq!(received.borrow().len(), 1);
    sim.close().unwrap();
    let (mut sim, _, _) = echo_sim(
        true,
        UartNetOptions {
            dispatch_budget: 4,
            ..Default::default()
        },
    );
    assert!(sim
        .start(None)
        .unwrap_err()
        .to_string()
        .contains("dispatcher budget exhausted"));
    assert_eq!(sim.state(), SimulationState::Failed);
}

#[test]
fn exact_two_endpoint_and_direction_admission_precedes_any_resource() {
    struct PeerFactory(Direction);
    impl Factory<HostOptions, HostInput, HostEvent> for PeerFactory {
        fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
            let mut port = PortSpec::new("uart", "uart");
            port.direction = self.0;
            Ok(NodeDescription::new(vec![port], []))
        }
        fn create(
            &self,
            _: &NodeSpec<HostOptions>,
            _: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
        ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
            panic!("admission must precede resources")
        }
    }
    for (count, direction) in [
        (1, Direction::InOut),
        (3, Direction::InOut),
        (2, Direction::In),
    ] {
        let mut registry = registry().unwrap();
        registry.register("peer", PeerFactory(direction)).unwrap();
        let mut nodes = vec![NodeSpec::new(
            "wire",
            "net.uart",
            HostOptions::UartNet(Default::default()),
        )];
        let mut links = vec![];
        for index in 0..count {
            nodes.push(NodeSpec::new(
                format!("peer{index}"),
                "peer",
                HostOptions::CanHost,
            ));
            links.push(link(&format!("peer{index}.uart"), "wire.uart"));
        }
        assert!(HostSimulation::new(
            Topology::new(nodes, links).unwrap(),
            registry,
            Default::default()
        )
        .is_err());
    }
    for options in [
        UartNetOptions {
            max_pending: 0,
            ..Default::default()
        },
        UartNetOptions {
            max_pending_bytes: 268435457,
            ..Default::default()
        },
    ] {
        assert!(UartNet::new(options, Rc::new(|_| Ok(()))).is_err());
    }
}

#[cfg(unix)]
#[test]
fn real_borrowed_stream_backpressure_keeps_physical_suffix_across_logical_reset_and_close() {
    use sim_host::transports::stream::StreamTransport;
    use std::{
        io::{self, Read, Write},
        os::unix::net::UnixStream,
    };
    let (mut socket, mut peer) = UnixStream::pair().unwrap();
    socket.set_nonblocking(true).unwrap();
    peer.set_nonblocking(true).unwrap();
    let mut prefilled = 0;
    let mut blocked = false;
    for _ in 0..4096 {
        match socket.write(&[0x55; 4096]) {
            Ok(length) => prefilled += length,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                blocked = true;
                break;
            }
            other => panic!("unexpected socket prefill: {other:?}"),
        }
    }
    assert!(
        blocked,
        "finite prefill must reach actual kernel backpressure"
    );
    let transport = Rc::new(RefCell::new(StreamTransport::new(socket, 1).unwrap()));
    let borrowed = transport.clone();
    let mut net = UartNet::new(Default::default(), Rc::new(|_| Ok(()))).unwrap();
    let source = UartEndpointId::new();
    net.attach(
        "source.uart".into(),
        UartAttachment {
            endpoint: source.clone(),
            receive: Rc::new(|_, _| Ok(())),
        },
    )
    .unwrap();
    net.attach(
        "sink.uart".into(),
        UartAttachment {
            endpoint: UartEndpointId::new(),
            receive: Rc::new(move |data, _| {
                borrowed
                    .borrow_mut()
                    .send(data.to_vec())
                    .map_err(|error| error as Failure)
            }),
        },
    )
    .unwrap();
    net.reset(Session::new(1, 0).unwrap()).unwrap();
    let bytes: Vec<_> = (0..4096).map(|index| (index % 251) as u8).collect();
    net.send(
        &source,
        &bytes,
        stamp(Some(700), Some(3), TimeQuality::UpperBound),
        3,
        3,
    )
    .unwrap();
    assert_eq!(net.dispatch(3, 1).unwrap(), 1);
    assert_eq!(net.observe().pending, 0);
    assert!(transport.borrow().tx_pending());
    net.reset(Session::new(2, 10).unwrap()).unwrap();
    net.close().unwrap();
    assert!(transport.borrow().tx_pending());

    let mut output = vec![];
    let mut buffer = [0; 4096];
    for _ in 0..4096 {
        loop {
            match peer.read(&mut buffer) {
                Ok(0) => panic!("logical net close must not close the borrowed stream"),
                Ok(length) => output.extend_from_slice(&buffer[..length]),
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                other => panic!("unexpected read: {other:?}"),
            }
        }
        transport.borrow_mut().flush().unwrap();
        if output.len() == prefilled + bytes.len() {
            break;
        }
    }
    let mut expected = vec![0x55; prefilled];
    expected.extend_from_slice(&bytes);
    assert_eq!(output, expected);
    assert!(!transport.borrow().tx_pending());
    peer.write_all(b"live").unwrap();
    transport.borrow_mut().read_once().unwrap();
    assert_eq!(
        transport.borrow_mut().next_record(4).unwrap(),
        Some(b"live".to_vec())
    );
}
