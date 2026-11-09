#![cfg(all(feature = "mujoco", feature = "simulation", feature = "devices"))]

use sim_core::{
    BuildContext, Component, Direction, Endpoint, EventPayload, Factory, LinkSpec, NodeDescription,
    NodeSpec, PortCategory, PortSpec, Session, SimulationOptions, SimulationState, StepContext,
    TimeQuality, Topology,
};
use sim_host::{
    can::{CanBus, CanBusOptions, CanCallbacks, CanFrame, EndpointId},
    devices::dji::{
        joints::{M3508JointPlant, M3508PlantBinding},
        DeviceOptions, M3508Device,
    },
    fabric::imu::ImuAttachment,
    physics::{
        factory::{M3508Options, MotorPlant, MujocoOptions, NativeWorld, WorldControl},
        mujoco::{MujocoBindings, MujocoPhysics},
        JointState,
    },
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
    signals::ImuSignalSample,
};
use std::{cell::RefCell, rc::Rc};

const ROTORS: &str = r#"<mujoco><compiler angle="radian"/>
  <option integrator="Euler" gravity="0 0 -9.81"/>
  <worldbody>
    <body name="left"><joint name="a" axis="0 0 1"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/>
      <site name="imu"/></body>
    <body name="right"><joint name="b" axis="0 0 1"/>
      <inertial pos="0 0 0" mass="1" diaginertia="0.015 0.015 0.02"/></body>
  </worldbody>
  <actuator><motor name="da" joint="a"/><motor name="db" joint="b"/></actuator>
  <sensor><gyro name="gyro" site="imu"/><accelerometer name="accel" site="imu"/></sensor>
</mujoco>"#;

fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}
fn link(a: &str, b: &str) -> LinkSpec {
    LinkSpec::new(ep(a), ep(b))
}
fn world(control: WorldControl) -> (tempfile::TempDir, NodeSpec<HostOptions>) {
    let directory = tempfile::tempdir().unwrap();
    let model = directory.path().join("rotors.xml");
    std::fs::write(&model, ROTORS).unwrap();
    let node = NodeSpec::new(
        "world",
        "physics.mujoco",
        HostOptions::Mujoco(MujocoOptions {
            model,
            control,
            bindings: MujocoBindings {
                joints: vec![("a".into(), "da".into()), ("b".into(), "db".into())],
                imus: vec![("imu".into(), "gyro".into(), "accel".into())],
            },
        }),
    );
    (directory, node)
}
fn simulation(
    nodes: Vec<NodeSpec<HostOptions>>,
    links: Vec<LinkSpec>,
    origin: u64,
) -> HostSimulation {
    HostSimulation::new(
        Topology::new(nodes, links).unwrap(),
        runtime::registry().unwrap(),
        SimulationOptions {
            quantum_ns: 10_000_000,
            initial_session: Session::new(7, origin).unwrap(),
            ..Default::default()
        },
    )
    .unwrap()
}
fn near(actual: f64, expected: f64) {
    assert!((actual - expected).abs() < 1e-12, "{actual} != {expected}");
}

#[test]
fn compiled_direct_world_preserves_native_epoch_completed_getters_and_input_admission() {
    let (_directory, node) = world(WorldControl::Direct);
    let origin = (1 << 63) + 17;
    let mut sim = simulation(vec![node], vec![], origin);
    sim.start(None).unwrap();
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    assert_eq!(sim.component(&handle).unwrap().time_s().unwrap(), 0.0);
    for input in [
        vec![],
        vec![("missing".into(), 0.4)],
        vec![("a".into(), f64::NAN)],
        vec![("a".into(), 0.4), ("a".into(), 0.2)],
    ] {
        assert!(sim
            .schedule_input(ep("world.effort"), HostInput::JointEffort(input), None)
            .is_err());
    }
    assert_eq!(sim.state(), SimulationState::Paused);
    sim.schedule_input(
        ep("world.effort"),
        HostInput::JointEffort(vec![("a".into(), 0.4)]),
        None,
    )
    .unwrap();
    sim.run_until(origin + 10_000_000, None).unwrap();
    let world = sim.component(&handle).unwrap();
    near(world.get_joint_state("a").unwrap().position_rad, 0.002);
    near(world.get_joint_state("a").unwrap().velocity_rad_s, 0.2);
    let sample = world.get_imu("imu").unwrap();
    assert_eq!(sample.accel_m_s2, [0.0, 0.0, 9.81]);
    near(sample.gyro_rad_s[2], 0.2);
    assert_eq!(world.time_s().unwrap(), 0.01);
    assert_eq!(world.get_imu("imu").unwrap(), sample);
    let event = sim
        .events()
        .into_iter()
        .find(|event| event.kind == "physics.state" && event.mapped_time_ns == origin + 10_000_000)
        .unwrap();
    assert_eq!(
        (event.source_time_ns, event.delivery_time_ns, event.quality),
        (None, origin + 10_000_000, TimeQuality::Sampled)
    );
    let EventPayload::Observation(HostEvent::PhysicsState(state)) = event.payload else {
        panic!("typed physics state")
    };
    assert_eq!(state.native_time_s, 0.01);
    assert_eq!(state.imus, [("imu".into(), sample)]);
    sim.reset(None).unwrap();
    assert!(sim.component(&handle).is_err());
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    assert_eq!(
        sim.component(&handle)
            .unwrap()
            .get_joint_state("a")
            .unwrap(),
        JointState {
            position_rad: 0.0,
            velocity_rad_s: 0.0
        }
    );
    sim.run_until(origin + 10_000_000, None).unwrap();
    assert_eq!(
        sim.component(&handle)
            .unwrap()
            .get_joint_state("a")
            .unwrap()
            .velocity_rad_s,
        0.0
    );
    sim.close().unwrap();
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
fn actual_native_imu_source_delivers_completed_si_to_the_shared_signal_net() {
    let (_directory, world) = world(WorldControl::Direct);
    let origin = (1 << 63) + 17;
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
        SimulationOptions {
            quantum_ns: 10_000_000,
            initial_session: Session::new(7, origin).unwrap(),
            ..Default::default()
        },
    )
    .unwrap();
    sim.start(None).unwrap();
    let initial = samples.borrow()[0];
    assert_eq!(initial.source_time_ns, None);
    assert_eq!(initial.mapped_time_ns, origin);
    assert_eq!(initial.sample.gyro_rad_s, [0.0; 3]);
    assert_eq!(initial.sample.accel_m_s2, [0.0, 0.0, 9.81]);
    sim.schedule_input(
        ep("world.effort"),
        HostInput::JointEffort(vec![("a".into(), 0.4)]),
        None,
    )
    .unwrap();
    sim.run_until(origin + 10_000_000, None).unwrap();
    assert_eq!(samples.borrow().len(), 2);
    let target = samples.borrow()[1];
    assert_eq!(target.source_time_ns, None);
    assert_eq!(target.mapped_time_ns, origin + 10_000_000);
    near(target.sample.gyro_rad_s[2], 0.2);
    assert_eq!(target.sample.accel_m_s2, [0.0, 0.0, 9.81]);
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    let world = sim.component(&handle).unwrap();
    assert_eq!(world.get_imu("imu").unwrap(), target.sample);
    assert_eq!(world.time_s().unwrap(), 0.01);
    let delivery = sim
        .events()
        .into_iter()
        .find(|event| {
            event.kind == "net.imu.delivery" && event.mapped_time_ns == target.mapped_time_ns
        })
        .unwrap();
    assert_eq!(
        (
            delivery.source_time_ns,
            delivery.delivery_time_ns,
            delivery.quality
        ),
        (None, target.mapped_time_ns, TimeQuality::Sampled)
    );
    let EventPayload::Observation(HostEvent::ImuDelivery { sample, .. }) = delivery.payload else {
        panic!("typed IMU delivery")
    };
    assert_eq!(sample, target);
    sim.reset(None).unwrap();
    assert_eq!(*samples.borrow().last().unwrap(), initial);
    sim.close().unwrap();
}

fn motor(name: &str, id: u8, joint: &str, gear_ratio: f64, timeout: u64) -> NodeSpec<HostOptions> {
    NodeSpec::new(
        name,
        "device.dji-m3508",
        HostOptions::M3508(M3508Options {
            device: DeviceOptions {
                motor_id: id,
                command_timeout_ns: timeout,
                ..Default::default()
            },
            plant: MotorPlant::Joint {
                joint: joint.into(),
                gear_ratio,
            },
        }),
    )
}
fn shared_nodes(world: NodeSpec<HostOptions>) -> Vec<NodeSpec<HostOptions>> {
    vec![
        world,
        NodeSpec::new(
            "bus",
            "bus.can",
            HostOptions::CanBus(CanBusOptions::default().into()),
        ),
        NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
        motor("left", 1, "a", 2.0, 3_000_000),
        motor("right", 2, "b", 1.0, 7_000_000),
    ]
}
fn shared_links() -> Vec<LinkSpec> {
    vec![
        link("host.can", "bus.can"),
        link("left.can", "bus.can"),
        link("right.can", "bus.can"),
        link("left.joint", "world.drives"),
        link("right.joint", "world.drives"),
    ]
}

#[test]
fn actual_shared_mujoco_and_can_devices_split_union_expiries_once_then_feedback() {
    let (_directory, node) = world(WorldControl::Bound);
    let origin = (1 << 63) + 17;
    let mut sim = simulation(shared_nodes(node), shared_links(), origin);
    sim.start(None).unwrap();
    // C620 slots1/2 are +10A/-10A; metadata supplied by caller is replaced by
    // the host's admitted input cut before the real CAN network delivers it.
    let command = CanFrame::new(0x200, 0, None, [0x20, 0, 0xe0, 0, 0, 0, 0, 0], 999).unwrap();
    sim.schedule_input(ep("host.tx"), HostInput::CanFrame(command), None)
        .unwrap();
    sim.run_until(origin + 10_000_000, None).unwrap();
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    let world = sim.component(&handle).unwrap();
    // I=.02, ideal gears2/1 => alpha=+20/-10. Three shared Euler intervals
    // [0,3),[3,7),[7,10)ms give these independently integrated targets.
    let a = world.get_joint_state("a").unwrap();
    let b = world.get_joint_state("b").unwrap();
    near(a.position_rad, 0.0006);
    near(a.velocity_rad_s, 0.06);
    near(b.position_rad, -0.00058);
    near(b.velocity_rad_s, -0.07);
    near(world.time_s().unwrap(), 0.01);
    let frames = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanHostReceived { frame })
                if frame.can_id() == 0x201 || frame.can_id() == 0x202 =>
            {
                Some(frame)
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(frames.len(), 2);
    assert_eq!(frames[0].data(), [0, 1, 0, 1, 0, 0, 25, 0]);
    assert_eq!(frames[1].data(), [0x1f, 0xff, 0xff, 0xff, 0, 0, 25, 0]);
    assert!(frames
        .iter()
        .all(|frame| frame.timestamp_ns() == origin + 10_000_000));
    sim.close().unwrap();
}

#[derive(Default)]
struct Delivered(Vec<CanFrame>);
impl CanCallbacks for Delivered {
    fn receive(
        &mut self,
        _: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> sim_host::Result<()> {
        self.0.push(frame.clone());
        Ok(())
    }
}

#[test]
fn owning_native_joint_plant_routes_two_borrowed_buses_without_advancing_them() {
    let (_directory, node) = world(WorldControl::Bound);
    let HostOptions::Mujoco(options) = node.options else {
        unreachable!()
    };
    let physics = MujocoPhysics::from_xml_path(options.model, options.bindings).unwrap();
    let origin = (1 << 63) + 17;
    let mut buses = vec![];
    let mut hosts = vec![];
    let mut devices = vec![];
    let mut bindings = vec![];
    for (joint, gear, timeout) in [("a", 2.0, 3_000_000), ("b", 1.0, 7_000_000)] {
        let mut bus = CanBus::new(CanBusOptions::default()).unwrap();
        let host = EndpointId::new();
        let motor = EndpointId::new();
        bus.attach(&host, "host").unwrap();
        bus.attach(&motor, "motor").unwrap();
        bus.reset(origin).unwrap();
        let bus = Rc::new(RefCell::new(bus));
        // ID1 on each distinct borrowed bus is valid.
        let device = Rc::new(RefCell::new(
            M3508Device::new(DeviceOptions {
                command_timeout_ns: timeout,
                ..Default::default()
            })
            .unwrap(),
        ));
        let send = bus.clone();
        bindings.push(M3508PlantBinding::new(
            device.clone(),
            &bus.borrow(),
            joint,
            gear,
            Box::new(move |frame| {
                send.borrow_mut()
                    .send(&motor, frame, None)
                    .map_err(|error| error as sim_core::Failure)
            }),
        ));
        buses.push(bus);
        hosts.push(host);
        devices.push(device);
    }
    let mut plant = M3508JointPlant::new(physics, bindings).unwrap();
    plant.reset(Session::new(7, origin).unwrap()).unwrap();
    for (index, bus) in buses.iter().enumerate() {
        bus.borrow_mut()
            .send(
                &hosts[index],
                CanFrame::new(
                    0x200,
                    0,
                    None,
                    if index == 0 {
                        [0x20, 0, 0, 0, 0, 0, 0, 0]
                    } else {
                        [0xe0, 0, 0, 0, 0, 0, 0, 0]
                    },
                    origin,
                )
                .unwrap(),
                None,
            )
            .unwrap();
        let mut delivered = Delivered::default();
        bus.borrow_mut().step(origin, None, &mut delivered).unwrap();
        assert_eq!(delivered.0.len(), 1);
        plant.ingress(&bus.borrow(), &delivered.0[0]).unwrap();
    }
    let result = plant
        .step(StepContext::new(7, 1, origin + 10_000_000, 10_000_000).unwrap())
        .unwrap();
    assert_eq!(result.diagnostics, Some(3));
    near(plant.get_joint_state("a").unwrap().position_rad, 0.0006);
    near(plant.get_joint_state("a").unwrap().velocity_rad_s, 0.06);
    near(plant.get_joint_state("b").unwrap().position_rad, -0.00058);
    near(plant.get_joint_state("b").unwrap().velocity_rad_s, -0.07);
    near(plant.get_imu("imu").unwrap().gyro_rad_s[2], 0.06);
    near(plant.time_s().unwrap(), 0.01);
    assert_eq!(plant.time_ns(), origin + 10_000_000);
    plant.close().unwrap();
    assert!(devices.iter().all(|device| device.borrow().is_closed()));
    for (index, bus) in buses.iter().enumerate() {
        assert_eq!(
            (bus.borrow().time_ns(), bus.borrow().pending()),
            (origin, 1)
        );
        let mut delivered = Delivered::default();
        bus.borrow_mut()
            .step(origin + 10_000_000, None, &mut delivered)
            .unwrap();
        assert_eq!(
            delivered.0,
            [CanFrame::new(
                0x201,
                0,
                None,
                if index == 0 {
                    [0, 1, 0, 1, 0, 0, 25, 0]
                } else {
                    [0x1f, 0xff, 0xff, 0xff, 0, 0, 25, 0]
                },
                origin + 10_000_000
            )
            .unwrap()]
        );
        bus.borrow_mut()
            .send(
                &hosts[index],
                CanFrame::new(1, 0, None, [1], origin + 10_000_000).unwrap(),
                None,
            )
            .unwrap();
        assert_eq!(bus.borrow().pending(), 1);
        bus.borrow_mut().close().unwrap();
    }
}

#[test]
fn duplicate_bus_ids_and_joint_bindings_are_rejected_before_native_xml_load() {
    for duplicate_id in [true, false] {
        let (_directory, node) = world(WorldControl::Bound);
        let HostOptions::Mujoco(options) = &node.options else {
            unreachable!()
        };
        std::fs::write(&options.model, "invalid XML must never be compiled").unwrap();
        let mut nodes = shared_nodes(node);
        let HostOptions::M3508(options) = &mut nodes[4].options else {
            unreachable!()
        };
        if duplicate_id {
            options.device.motor_id = 1;
        } else {
            options.plant = MotorPlant::Joint {
                joint: "a".into(),
                gear_ratio: 1.0,
            };
        }
        let error = match HostSimulation::new(
            Topology::new(nodes, shared_links()).unwrap(),
            runtime::registry().unwrap(),
            SimulationOptions::default(),
        ) {
            Err(error) => error,
            Ok(_) => panic!("pure duplicate admission"),
        };
        assert!(
            error.to_string().contains(if duplicate_id {
                "unique on each CAN bus"
            } else {
                "declared and distinct"
            }),
            "{error}"
        );
    }
}

#[test]
fn fixed_and_rigid_rotor_factories_use_the_same_c620_device_consumer() {
    for plant in [
        MotorPlant::FixedRotor {
            position_rad: 0.0,
            velocity_rad_s: 0.0,
        },
        MotorPlant::RigidRotor(sim_host::physics::rotor::RotorOptions {
            damping: 0.0,
            ..Default::default()
        }),
    ] {
        let fixed = matches!(plant, MotorPlant::FixedRotor { .. });
        let mut sim = simulation(
            vec![
                NodeSpec::new(
                    "bus",
                    "bus.can",
                    HostOptions::CanBus(CanBusOptions::default().into()),
                ),
                NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
                NodeSpec::new(
                    "motor",
                    "device.dji-m3508",
                    HostOptions::M3508(M3508Options {
                        device: DeviceOptions::default(),
                        plant,
                    }),
                ),
            ],
            vec![link("host.can", "bus.can"), link("motor.can", "bus.can")],
            0,
        );
        sim.start(None).unwrap();
        sim.schedule_input(
            ep("host.tx"),
            HostInput::CanFrame(
                CanFrame::new(0x200, 0, None, [0x40, 0, 0, 0, 0, 0, 0, 0], 0).unwrap(),
            ),
            None,
        )
        .unwrap();
        sim.run_until(10_000_000, None).unwrap();
        let frame = sim
            .events()
            .into_iter()
            .find_map(|event| match event.payload {
                EventPayload::Observation(HostEvent::CanHostReceived { frame }) => Some(frame),
                _ => None,
            })
            .unwrap();
        assert_eq!(
            &frame.data()[..4],
            if fixed {
                &[0, 0, 0, 0][..]
            } else {
                &[0, 2, 0, 2][..]
            }
        );
        assert_eq!(&frame.data()[4..], [0x40, 0, 25, 0]);
        sim.close().unwrap();
    }
}

#[test]
fn native_solver_failure_freezes_simulation_at_last_commit_with_original_error() {
    if std::env::var_os("SIM_MUJOCO_SIMULATION_FAILURE_CHILD").is_none() {
        let directory = tempfile::tempdir().unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "native_solver_failure_freezes_simulation_at_last_commit_with_original_error",
                "--nocapture",
            ])
            .env("SIM_MUJOCO_SIMULATION_FAILURE_CHILD", "1")
            .current_dir(directory.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    let (_directory, node) = world(WorldControl::Direct);
    let mut sim = simulation(vec![node], vec![], 100);
    sim.start(None).unwrap();
    sim.schedule_input(
        ep("world.effort"),
        HostInput::JointEffort(vec![("a".into(), 1e308)]),
        None,
    )
    .unwrap();
    let primary = sim.run_until(10_000_100, None).unwrap_err();
    assert!(primary.to_string().contains("mjWARN_BADCTRL"));
    assert_eq!(
        (sim.state(), sim.t_sim_ns()),
        (SimulationState::Failed, 100)
    );
    assert!(std::sync::Arc::ptr_eq(&primary, &sim.failure().unwrap()));
    let handle = sim.handle::<NativeWorld>("world").unwrap();
    assert!(std::sync::Arc::ptr_eq(
        &primary,
        &sim.component(&handle).unwrap().time_s().unwrap_err()
    ));
    assert!(std::sync::Arc::ptr_eq(
        &primary,
        &sim.schedule_input(
            ep("world.effort"),
            HostInput::JointEffort(vec![("a".into(), 0.0)]),
            None
        )
        .unwrap_err()
    ));
    sim.close().unwrap();
}
