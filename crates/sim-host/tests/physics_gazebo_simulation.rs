#![cfg(all(feature = "gazebo", feature = "simulation", feature = "devices"))]

use sim_core::{
    BuildContext, Component, Direction, Endpoint, EventPayload, Factory, LinkSpec, NodeDescription,
    NodeSpec, PortCategory, PortSpec, Session, SimulationOptions, SimulationState, TimeQuality,
    Topology,
};
use sim_host::{
    can::{CanBusOptions, CanFrame},
    devices::dji::DeviceOptions,
    fabric::imu::ImuAttachment,
    physics::{
        factory::{GazeboOptions, M3508Options, MotorPlant, NativeWorld, WorldControl},
        gazebo::GazeboBindings,
    },
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
    signals::ImuSignalSample,
};
use std::{cell::RefCell, rc::Rc};

const ORIGIN: u64 = (1 << 63) + 17;
fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}
fn link(a: &str, b: &str) -> LinkSpec {
    LinkSpec::new(ep(a), ep(b))
}
fn world(control: WorldControl) -> (tempfile::TempDir, NodeSpec<HostOptions>) {
    let directory = tempfile::tempdir().unwrap();
    let model = directory.path().join("rotors.sdf");
    let source = include_str!("../../../models/gazebo_imu_rotor.sdf");
    let start = source.find("<model name=\"rotor\">").unwrap();
    let end = source.find("</model>").unwrap() + 8;
    let rotor = &source[start..end];
    let sensor_start = rotor.find("<sensor ").unwrap();
    let sensor_end = rotor.find("</sensor>").unwrap() + 9;
    let right = rotor
        .replace(&rotor[sensor_start..sensor_end], "")
        .replace("name=\"rotor\"", "name=\"right\"");
    std::fs::write(
        &model,
        source.replace(
            rotor,
            &format!(
                "{}{}",
                rotor.replace("name=\"rotor\"", "name=\"left\""),
                right
            ),
        ),
    )
    .unwrap();
    let node = NodeSpec::new(
        "world",
        "physics.gazebo",
        HostOptions::Gazebo(GazeboOptions {
            model,
            control,
            resource_paths: vec![],
            bindings: GazeboBindings {
                joints: vec![
                    ("a".into(), "left".into(), "shaft".into()),
                    ("b".into(), "right".into(), "shaft".into()),
                ],
                imus: vec![("imu".into(), "left".into(), "body".into(), "imu".into())],
                ..Default::default()
            },
        }),
    );
    (directory, node)
}
fn simulation_options() -> SimulationOptions {
    SimulationOptions {
        quantum_ns: 10_000_000,
        initial_session: Session::new(7, ORIGIN).unwrap(),
        ..Default::default()
    }
}
fn near(actual: f64, expected: f64, tolerance: f64) {
    assert!(
        (actual - expected).abs() <= tolerance,
        "{actual} != {expected} within {tolerance}"
    );
}

struct ImuSink;
impl Component<HostInput, HostEvent> for ImuSink {
    fn close(&mut self) -> sim_core::Result<()> {
        Ok(())
    }
}
struct ImuSinkFactory(Rc<RefCell<Vec<ImuSignalSample>>>);
impl Factory<HostOptions, HostInput, HostEvent> for ImuSinkFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> sim_core::Result<NodeDescription> {
        let mut port = PortSpec::new("imu", "imu");
        port.category = PortCategory::Signal;
        port.direction = Direction::In;
        Ok(NodeDescription::new(vec![port], []))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> sim_core::Result<Box<dyn Component<HostInput, HostEvent>>> {
        let samples = self.0.clone();
        build.attach::<_, ()>(
            "imu",
            ImuAttachment::Sink(Rc::new(move |sample| {
                samples.borrow_mut().push(sample);
                Ok(())
            })),
        )?;
        Ok(Box::new(ImuSink))
    }
}

#[test]
fn compiled_gazebo_effort_and_imu_net_publish_only_completed_native_state() {
    let (_directory, world) = world(WorldControl::Direct);
    let samples = Rc::new(RefCell::new(vec![]));
    let mut registry = runtime::registry().unwrap();
    registry
        .register("fixture.imu-sink", ImuSinkFactory(samples.clone()))
        .unwrap();
    let mut sim = HostSimulation::new(
        Topology::new(
            vec![
                world,
                NodeSpec::new("wire", "net.imu", HostOptions::ImuNet(Default::default())),
                NodeSpec::new("sink", "fixture.imu-sink", HostOptions::CanHost),
            ],
            vec![
                link("world.imu.imu", "wire.signal"),
                link("sink.imu", "wire.signal"),
            ],
        )
        .unwrap(),
        registry,
        simulation_options(),
    )
    .unwrap();
    sim.start(None).unwrap();
    assert_eq!(samples.borrow()[0].mapped_time_ns, ORIGIN);
    sim.schedule_input(
        ep("world.effort"),
        HostInput::JointEffort(vec![("a".into(), 0.4)]),
        None,
    )
    .unwrap();
    sim.run_until(ORIGIN + 10_000_000, None).unwrap();
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    let world = sim.component(&handle).unwrap();
    // tau/I=.4/.02=20rad/s². Native integration uses ten1ms ticks.
    near(
        world.get_joint_state("a").unwrap().position_rad,
        0.001,
        0.00021,
    );
    near(
        world.get_joint_state("a").unwrap().velocity_rad_s,
        0.2,
        1e-10,
    );
    assert_eq!(world.time_s().unwrap(), 0.01);
    assert_eq!(samples.borrow().len(), 2);
    let target = samples.borrow()[1];
    assert_eq!(
        (target.source_time_ns, target.mapped_time_ns),
        (None, ORIGIN + 10_000_000)
    );
    near(target.sample.gyro_rad_s[2], 0.2, 1e-10);
    assert_eq!(target.sample.accel_m_s2, [0.0, 0.0, 9.80665]);
    assert_eq!(world.get_imu("imu").unwrap(), target.sample);
    let event = sim
        .events()
        .into_iter()
        .find(|event| event.kind == "physics.state" && event.mapped_time_ns == ORIGIN + 10_000_000)
        .unwrap();
    assert_eq!(
        (event.source_time_ns, event.delivery_time_ns, event.quality),
        (None, ORIGIN + 10_000_000, TimeQuality::Sampled)
    );
    let EventPayload::Observation(HostEvent::PhysicsState(state)) = event.payload else {
        panic!("typed physics state")
    };
    assert_eq!(state.native_time_s, 0.01);
    assert_eq!(state.imus, [("imu".into(), target.sample)]);
    sim.reset(None).unwrap();
    assert!(sim.component(&handle).is_err());
    let reset = sim.handle::<NativeWorld>("world").unwrap();
    assert_eq!(sim.component(&reset).unwrap().time_s().unwrap(), 0.0);
    assert_eq!(samples.borrow().last().unwrap().sample.gyro_rad_s, [0.0; 3]);
    sim.close().unwrap();
}

fn shared(
    world: NodeSpec<HostOptions>,
    timeout: u64,
) -> (Vec<NodeSpec<HostOptions>>, Vec<LinkSpec>) {
    let mut nodes = vec![
        world,
        NodeSpec::new(
            "bus",
            "bus.can",
            HostOptions::CanBus(CanBusOptions::default().into()),
        ),
        NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
    ];
    for (name, id, joint, gear, expiry) in [
        ("left", 1, "a", 2.0, timeout),
        ("right", 2, "b", 1.0, 7_000_000),
    ] {
        nodes.push(NodeSpec::new(
            name,
            "device.dji-m3508",
            HostOptions::M3508(M3508Options {
                device: DeviceOptions {
                    motor_id: id,
                    command_timeout_ns: expiry,
                    ..Default::default()
                },
                plant: MotorPlant::Joint {
                    joint: joint.into(),
                    gear_ratio: gear,
                },
            }),
        ));
    }
    (
        nodes,
        vec![
            link("host.can", "bus.can"),
            link("left.can", "bus.can"),
            link("right.can", "bus.can"),
            link("left.joint", "world.drives"),
            link("right.joint", "world.drives"),
        ],
    )
}
fn command(sim: &mut HostSimulation) {
    sim.schedule_input(
        ep("host.tx"),
        HostInput::CanFrame(
            CanFrame::new(0x200, 0, None, [0x20, 0, 0xe0, 0, 0, 0, 0, 0], 999).unwrap(),
        ),
        None,
    )
    .unwrap();
}

#[test]
fn compiled_gazebo_shared_c620_consumers_split_expiries_and_deliver_exact_wire_feedback() {
    let (_directory, world) = world(WorldControl::Bound);
    let (nodes, links) = shared(world, 3_000_000);
    let mut sim = HostSimulation::new(
        Topology::new(nodes, links).unwrap(),
        runtime::registry().unwrap(),
        simulation_options(),
    )
    .unwrap();
    sim.start(None).unwrap();
    command(&mut sim);
    sim.run_until(ORIGIN + 10_000_000, None).unwrap();
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    let world = sim.component(&handle).unwrap();
    // Ideal gears2/1 and I=.02 give alpha+20/-10 until3/7ms, then coast.
    // Continuous targets allow one native1ms integration tick of position.
    for (name, theta, omega) in [("a", 0.00051, 0.06_f64), ("b", -0.000455, -0.07)] {
        let state = world.get_joint_state(name).unwrap();
        near(state.position_rad, theta, omega.abs() * 0.0011);
        near(state.velocity_rad_s, omega, 1e-10);
    }
    assert_eq!(world.time_s().unwrap(), 0.01);
    let feedback = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanHostReceived { frame })
                if matches!(frame.can_id(), 0x201 | 0x202) =>
            {
                Some(frame)
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        feedback,
        [
            CanFrame::new(
                0x201,
                0,
                None,
                [0, 1, 0, 1, 0, 0, 25, 0],
                ORIGIN + 10_000_000
            )
            .unwrap(),
            CanFrame::new(
                0x202,
                0,
                None,
                [0x1f, 0xff, 0xff, 0xff, 0, 0, 25, 0],
                ORIGIN + 10_000_000
            )
            .unwrap(),
        ]
    );
    sim.close().unwrap();
}

#[test]
fn compiled_gazebo_admission_rejects_topology_and_resources_before_sdf_acquisition() {
    for invalid in ["duplicate", "resources", "imu-port"] {
        let (_directory, mut world) = world(WorldControl::Bound);
        let HostOptions::Gazebo(options) = &mut world.options else {
            unreachable!()
        };
        std::fs::write(&options.model, "invalid SDF must never be parsed").unwrap();
        if invalid == "resources" {
            options.resource_paths.push(options.model.clone());
        } else if invalid == "imu-port" {
            options.bindings.imus[0].0 = "nested.port".into();
        }
        let (mut nodes, links) = shared(world, 3_000_000);
        if invalid == "duplicate" {
            let HostOptions::M3508(options) = &mut nodes[4].options else {
                unreachable!()
            };
            options.plant = MotorPlant::Joint {
                joint: "a".into(),
                gear_ratio: 1.0,
            };
        }
        let error = match HostSimulation::new(
            Topology::new(nodes, links).unwrap(),
            runtime::registry().unwrap(),
            simulation_options(),
        ) {
            Err(error) => error,
            Ok(_) => panic!("pure factory admission"),
        };
        let expected = match invalid {
            "duplicate" => "declared and distinct",
            "resources" => "directory",
            _ => "single port identifier",
        };
        assert!(error.to_string().contains(expected), "{error}");
    }
}

#[test]
fn compiled_gazebo_off_grid_expiry_fails_without_publishing_a_completed_cut() {
    let (_directory, world) = world(WorldControl::Bound);
    let (nodes, links) = shared(world, 3_000_001);
    let mut sim = HostSimulation::new(
        Topology::new(nodes, links).unwrap(),
        runtime::registry().unwrap(),
        simulation_options(),
    )
    .unwrap();
    sim.start(None).unwrap();
    command(&mut sim);
    let error = sim.run_until(ORIGIN + 10_000_000, None).unwrap_err();
    assert!(error.to_string().contains("step"), "{error}");
    assert_eq!(sim.state(), SimulationState::Failed);
    assert_eq!(sim.session().unwrap().t_sim_ns, ORIGIN);
    assert!(sim
        .events()
        .iter()
        .all(|event| event.kind != "physics.state" || event.mapped_time_ns == ORIGIN));
    assert!(sim.run_until(ORIGIN + 10_000_000, None).is_err());
    sim.close().unwrap();
}
