#![cfg(feature = "simulation")]
use sim_core::{Endpoint, EventPayload, SimulationOptions, SimulationState};
use sim_host::{
    can::{CanBusOptions, CanFrame},
    runtime::{host_can, CanHost, CanNetwork, HostEvent, HostInput},
};
use std::{rc::Rc, sync::Arc};
fn input(id: u32) -> HostInput {
    HostInput::CanFrame(CanFrame::new(id, 0, None, [0x5a], 999).unwrap())
}
fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}

#[test]
fn real_host_can_factories_keep_off_grid_source_and_delivery_times_and_ties() {
    let mut sim = host_can(
        CanBusOptions {
            delay_ns: 2,
            ..Default::default()
        },
        SimulationOptions {
            quantum_ns: 10,
            ..Default::default()
        },
    )
    .unwrap();
    sim.start(None).unwrap();
    for (at, id) in [(3, 0x123), (7, 0x124), (7, 0x125)] {
        sim.schedule_input(ep("left.tx"), input(id), Some(at))
            .unwrap();
    }
    let result = sim.run_until(22, None).unwrap();
    assert_eq!(result.substeps_completed, 6);
    let deliveries: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanDelivery {
                destination,
                frame,
                sequence,
                due_ns,
                late,
                dropped,
            }) => Some((
                destination,
                frame.can_id(),
                sequence,
                event.source_time_ns,
                event.mapped_time_ns,
                event.delivery_time_ns,
                due_ns,
                late,
                dropped,
            )),
            _ => None,
        })
        .collect();
    assert_eq!(
        deliveries,
        vec![
            ("right.can".into(), 0x123, 0, Some(3), 3, 5, 5, false, false),
            ("right.can".into(), 0x124, 1, Some(7), 7, 9, 9, false, false),
            ("right.can".into(), 0x125, 2, Some(7), 7, 9, 9, false, false)
        ]
    );
    let received: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanHostReceived { frame }) => {
                Some((event.source_node, frame.timestamp_ns()))
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        received,
        [
            ("right".into(), 5),
            ("right".into(), 9),
            ("right".into(), 9)
        ]
    );
    sim.close().unwrap();
}

#[test]
fn observer_can_schedule_response_and_reset_recreates_real_bus() {
    let mut sim = host_can(CanBusOptions::default(), SimulationOptions::default()).unwrap();
    sim.start(None).unwrap();
    sim.set_observer(Some(Rc::new(|event, control| {
        if event.source_node == "right" && event.kind == "can-host.received" {
            control.schedule_input(ep("right.tx"), input(0x321), None)?;
        }
        Ok(())
    })));
    sim.schedule_input(ep("left.tx"), input(0x123), Some(3))
        .unwrap();
    sim.run_until(3, None).unwrap();
    let received: Vec<_> = sim
        .events()
        .into_iter()
        .filter(|event| event.kind == "can-host.received")
        .map(|event| event.source_node)
        .collect();
    assert_eq!(received, ["right", "left"]);
    let old = sim.handle::<CanNetwork>("bus").unwrap();
    sim.set_observer(None);
    sim.reset(None).unwrap();
    assert!(sim.component(&old).is_err());
    assert_eq!(sim.session().unwrap().session_id, 2);
    let host = sim.handle::<CanHost>("right").unwrap();
    sim.with_component(&host, |host, _| {
        host.send(CanFrame::new(0x400, 0, None, [], 888).unwrap())
    })
    .unwrap();
    sim.run_until(0, None).unwrap();
    assert_eq!(sim.events().last().unwrap().source_node, "left");
    sim.close().unwrap();
}

#[test]
fn paused_can_capacity_failure_immediately_freezes_core_with_same_allocation() {
    let mut sim = host_can(
        CanBusOptions {
            max_pending: 1,
            delay_ns: 10,
            ..Default::default()
        },
        SimulationOptions::default(),
    )
    .unwrap();
    sim.start(None).unwrap();
    let host = sim.handle::<CanHost>("left").unwrap();
    sim.with_component(&host, |host, _| {
        host.send(CanFrame::new(1, 0, None, [], 0).unwrap())
    })
    .unwrap();
    let error = sim
        .with_component(&host, |host, _| {
            host.send(CanFrame::new(2, 0, None, [], 0).unwrap())
        })
        .unwrap_err();
    assert_eq!(sim.state(), SimulationState::Failed);
    assert!(Arc::ptr_eq(&error, &sim.failure().unwrap()));
    assert_eq!(sim.t_sim_ns(), 0);
    assert!(Arc::ptr_eq(
        &sim.schedule_input(ep("right.tx"), input(3), None)
            .unwrap_err(),
        &error
    ));
    assert!(sim.step(None, None).is_err());
    sim.close().unwrap();
}

#[test]
fn fault_inputs_preserve_pending_deadlines_apply_recipient_drop_and_reset_normal() {
    use sim_host::runtime::{CanDropRule, CanFaultProfile, CanNetworkOptions};
    let options = CanNetworkOptions {
        bus: CanBusOptions {
            delay_ns: 4,
            ..Default::default()
        },
        fault_profiles: vec![
            CanFaultProfile {
                name: "lag".into(),
                delay_ns: Some(10),
                drop: vec![],
            },
            CanFaultProfile {
                name: "cut".into(),
                delay_ns: Some(0),
                drop: vec![CanDropRule {
                    source: Some(ep("left.can")),
                    destination: Some(ep("right.can")),
                    can_id: Some(0x123),
                }],
            },
        ],
    };
    let mut sim = host_can(
        options,
        SimulationOptions {
            quantum_ns: 10,
            ..Default::default()
        },
    )
    .unwrap();
    sim.start(None).unwrap();
    for (endpoint, value, when) in [
        ("left.tx", input(0x123), 0),
        ("bus.fault", HostInput::FaultProfile("lag".into()), 1),
        ("left.tx", input(0x101), 2),
        ("bus.fault", HostInput::FaultProfile("cut".into()), 3),
        ("left.tx", input(0x123), 3),
        ("left.tx", input(0x124), 3),
        ("bus.fault", HostInput::FaultProfile("normal".into()), 5),
        ("left.tx", input(0x125), 6),
    ] {
        sim.schedule_input(ep(endpoint), value, Some(when)).unwrap();
    }
    sim.run_until(15, None).unwrap();
    let deliveries: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanDelivery {
                frame,
                sequence,
                due_ns,
                dropped,
                ..
            }) => Some((
                frame.can_id(),
                sequence,
                due_ns,
                event.delivery_time_ns,
                dropped,
            )),
            _ => None,
        })
        .collect();
    assert_eq!(
        deliveries,
        [
            (0x123, 2, 3, 3, true),
            (0x124, 3, 3, 3, false),
            (0x123, 0, 4, 4, true),
            (0x125, 4, 10, 10, false),
            (0x101, 1, 12, 12, false)
        ]
    );
    let received: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanHostReceived { frame }) => Some(frame.can_id()),
            _ => None,
        })
        .collect();
    assert_eq!(received, [0x124, 0x125, 0x101]);
    let policies: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanFaultApplied {
                previous_profile,
                profile,
                delay_ns,
                cut_ns,
                ..
            }) => Some((previous_profile, profile, delay_ns, cut_ns)),
            _ => None,
        })
        .collect();
    assert_eq!(
        policies,
        [
            ("normal".into(), "lag".into(), 10, 1),
            ("lag".into(), "cut".into(), 0, 3),
            ("cut".into(), "normal".into(), 4, 5)
        ]
    );
    let bus = sim.handle::<CanNetwork>("bus").unwrap();
    sim.with_component(&bus, |bus, _| bus.apply_profile("lag"))
        .unwrap();
    let host = sim.handle::<CanHost>("left").unwrap();
    sim.with_component(&host, |host, _| {
        host.send(CanFrame::new(0x200, 0, None, [], 0).unwrap())
    })
    .unwrap();
    assert_eq!(sim.component(&bus).unwrap().pending(), 1);
    sim.reset(None).unwrap();
    let bus = sim.handle::<CanNetwork>("bus").unwrap();
    assert_eq!(sim.component(&bus).unwrap().profile(), "normal");
    assert_eq!(sim.component(&bus).unwrap().pending(), 0);
    sim.schedule_input(ep("left.tx"), input(0x201), Some(0))
        .unwrap();
    sim.run_until(4, None).unwrap();
    assert!(
        matches!(&sim.events().last().unwrap().payload,EventPayload::Observation(HostEvent::CanHostReceived{frame}) if frame.timestamp_ns()==4)
    );
    sim.close().unwrap();
}

#[test]
fn fault_admission_rejects_invalid_policies_and_unknown_stimulus_without_freezing() {
    use sim_host::runtime::{CanDropRule, CanFaultProfile, CanNetworkOptions};
    let profile = CanFaultProfile {
        name: "loss".into(),
        delay_ns: None,
        drop: vec![],
    };
    let good = CanNetworkOptions {
        bus: CanBusOptions::default(),
        fault_profiles: vec![profile.clone()],
    };
    let mut invalid = vec![];
    let mut option = good.clone();
    option.fault_profiles[0].name = "normal".into();
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles.push(profile);
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles[0].name = "bad.name".into();
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles[0].drop = vec![CanDropRule::default()];
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles[0].drop = vec![CanDropRule {
        can_id: Some(0x2000_0000),
        ..Default::default()
    }];
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles[0].drop = vec![CanDropRule {
        source: Some(ep("foreign.can")),
        ..Default::default()
    }];
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles[0].drop = vec![
        CanDropRule {
            can_id: Some(1),
            ..Default::default()
        };
        65
    ];
    invalid.push(option);
    let mut option = good.clone();
    option.fault_profiles = (0..17)
        .map(|i| CanFaultProfile {
            name: format!("profile{i}"),
            delay_ns: None,
            drop: vec![],
        })
        .collect();
    invalid.push(option);
    let mut option = good.clone();
    option.bus.max_pending = 65537;
    invalid.push(option);
    for option in invalid {
        assert!(host_can(option, SimulationOptions::default()).is_err());
    }
    let mut sim = host_can(good, SimulationOptions::default()).unwrap();
    sim.start(None).unwrap();
    assert!(sim
        .schedule_input(
            ep("bus.fault"),
            HostInput::FaultProfile("unknown".into()),
            None
        )
        .is_err());
    assert!(sim.schedule_input(ep("bus.fault"), input(1), None).is_err());
    assert!(sim
        .schedule_input(
            ep("left.tx"),
            HostInput::FaultProfile("normal".into()),
            None
        )
        .is_err());
    assert_eq!(sim.state(), SimulationState::Paused);
    assert!(sim.failure().is_none());
    sim.schedule_input(
        ep("bus.fault"),
        HostInput::FaultProfile("loss".into()),
        None,
    )
    .unwrap();
    sim.run_until(0, None).unwrap();
    sim.close().unwrap();
}

#[test]
fn drop_rule_requires_every_field_and_only_removes_matching_recipient() {
    use sim_core::{LinkSpec, NodeSpec, Topology};
    use sim_host::runtime::{
        registry, CanDropRule, CanFaultProfile, CanNetworkOptions, HostOptions, HostSimulation,
    };
    let options = CanNetworkOptions {
        bus: CanBusOptions::default(),
        fault_profiles: vec![CanFaultProfile {
            name: "selective".into(),
            delay_ns: None,
            drop: vec![CanDropRule {
                source: Some(ep("left.can")),
                destination: Some(ep("right.can")),
                can_id: Some(0x123),
            }],
        }],
    };
    let nodes = vec![
        NodeSpec::new("bus", "bus.can", HostOptions::CanBus(options)),
        NodeSpec::new("left", "device.can-host", HostOptions::CanHost),
        NodeSpec::new("right", "device.can-host", HostOptions::CanHost),
        NodeSpec::new("third", "device.can-host", HostOptions::CanHost),
    ];
    let links = ["left.can", "right.can", "third.can"]
        .into_iter()
        .map(|source| LinkSpec::new(ep(source), ep("bus.can")))
        .collect();
    let mut sim = HostSimulation::new(
        Topology::new(nodes, links).unwrap(),
        registry().unwrap(),
        SimulationOptions::default(),
    )
    .unwrap();
    sim.start(None).unwrap();
    for (endpoint, value) in [
        ("bus.fault", HostInput::FaultProfile("selective".into())),
        ("left.tx", input(0x123)),
        ("third.tx", input(0x123)),
        ("left.tx", input(0x124)),
    ] {
        sim.schedule_input(ep(endpoint), value, Some(1)).unwrap();
    }
    sim.run_until(1, None).unwrap();
    let recipients: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::CanDelivery {
                destination,
                frame,
                dropped,
                ..
            }) => Some((event.source_node, destination, frame.can_id(), dropped)),
            _ => None,
        })
        .collect();
    assert_eq!(
        recipients,
        [
            ("left".into(), "right.can".into(), 0x123, true),
            ("left".into(), "third.can".into(), 0x123, false),
            ("third".into(), "left.can".into(), 0x123, false),
            ("third".into(), "right.can".into(), 0x123, false),
            ("left".into(), "right.can".into(), 0x124, false),
            ("left".into(), "third.can".into(), 0x124, false)
        ]
    );
    sim.close().unwrap();
}
