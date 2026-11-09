#![cfg(all(feature = "referee", feature = "simulation"))]
#[path = "support/referee.rs"]
mod support;
use sim_core::{
    Endpoint, EventPayload, LinkSpec, NodeSpec, SimulationOptions, TimeQuality, Topology,
};
use sim_host::{
    devices::referee::{
        decode_frame,
        factory::{RefereeDevice, RefereeOptions},
        Hit, MatchDurations, RefereeConfiguration, RefereeEvent, ScriptEvent,
    },
    runtime::{registry, HostEvent, HostInput, HostOptions, HostSimulation},
};
use std::{rc::Rc, sync::Arc};
fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}
fn topology() -> Topology<HostOptions> {
    Topology::new(
        vec![
            NodeSpec::new(
                "referee",
                "device.referee",
                HostOptions::Referee(RefereeOptions {
                    configuration: RefereeConfiguration {
                        initial: support::state(),
                        durations: MatchDurations {
                            countdown_s: 3,
                            battle_s: 4,
                        },
                        events: vec![ScriptEvent {
                            offset_ns: 5_000_000,
                            value: RefereeEvent::Hit(Hit { damage_hp: 20 }),
                        }],
                        cooling_42mm_per_s: 2,
                    },
                    period_ns: 10_000_000,
                }),
            ),
            NodeSpec::new("wire", "net.uart", HostOptions::UartNet(Default::default())),
            NodeSpec::new("peer", "device.uart-echo", HostOptions::UartEcho),
        ],
        vec![
            LinkSpec::new(ep("referee.uart"), ep("wire.uart")),
            LinkSpec::new(ep("peer.uart"), ep("wire.uart")),
        ],
    )
    .unwrap()
}
fn frames(mut wire: &[u8]) -> Vec<sim_host::devices::referee::Frame> {
    let mut result = vec![];
    while !wire.is_empty() {
        let n = u16::from_le_bytes([wire[1], wire[2]]) as usize + 9;
        result.push(decode_frame(&wire[..n]).unwrap());
        wire = &wire[n..];
    }
    result
}
#[test]
fn actual_simulation_splits_at_periodic_deadlines_and_delivers_completed_engine_bytes() {
    let mut sim = HostSimulation::new(
        topology(),
        registry().unwrap(),
        SimulationOptions {
            quantum_ns: 50_000_000,
            ..Default::default()
        },
    )
    .unwrap();
    sim.start(None).unwrap();
    assert!(sim
        .schedule_input(
            ep("referee.enable"),
            HostInput::RefereeEnable(false),
            Some(0)
        )
        .is_err());
    for at in [3_000_000, 8_000_000] {
        sim.schedule_input(
            ep("referee.enable"),
            HostInput::RefereeEnable(true),
            Some(at),
        )
        .unwrap();
    }
    sim.run_until(35_000_000, None).unwrap();
    let batches: Vec<_> = sim
        .events()
        .into_iter()
        .filter_map(|event| match event.payload {
            EventPayload::Observation(HostEvent::RefereeBatch {
                data,
                first_sequence,
                next_sequence,
                publication,
                period_ns,
                due_ns,
                next_due_ns,
            }) => Some((
                data,
                first_sequence,
                next_sequence,
                publication,
                period_ns,
                due_ns,
                next_due_ns,
                event.source_time_ns,
                event.mapped_time_ns,
                event.delivery_time_ns,
                event.quality,
            )),
            _ => None,
        })
        .collect();
    assert_eq!(batches.len(), 4);
    for (index, batch) in batches.iter().enumerate() {
        let cut = 3_000_000 + index as u64 * 10_000_000;
        assert_eq!(
            (
                batch.1, batch.2, batch.3, batch.4, batch.5, batch.6, batch.7, batch.8, batch.9,
                batch.10
            ),
            (
                index as u8 * 4,
                index as u8 * 4 + 4,
                index as u64 + 1,
                10_000_000,
                cut,
                cut + 10_000_000,
                Some(cut),
                cut,
                cut,
                TimeQuality::Exact
            )
        );
        let frames = frames(&batch.0);
        assert_eq!(
            frames
                .iter()
                .map(|frame| frame.command_id)
                .collect::<Vec<_>>(),
            [1, 0x201, 0x202, 0x204]
        );
        // HP direct wire at the completed cut: event at 5ms affects later13ms batch.
        assert_eq!(
            &frames[1].payload[2..4],
            if index == 0 { &[100, 0] } else { &[80, 0] }
        );
    }
    let deliveries = sim
        .events()
        .iter()
        .filter(|event| event.kind == "net.uart.delivery")
        .count();
    assert_eq!(deliveries, 8); // each batch is echoed once; referee ignores reverse bytes.
    let handle = sim.handle::<RefereeDevice>("referee").unwrap();
    assert_eq!(sim.component(&handle).unwrap().publications(), 4);
    sim.reset(None).unwrap();
    let handle = sim.handle::<RefereeDevice>("referee").unwrap();
    assert_eq!(
        (
            sim.component(&handle).unwrap().publications(),
            sim.component(&handle).unwrap().next_sequence()
        ),
        (0, 0)
    );
    sim.close().unwrap();
}
#[test]
fn observer_failure_keeps_admitted_partial_effects_and_original_error() {
    let mut sim = HostSimulation::new(
        topology(),
        registry().unwrap(),
        SimulationOptions::default(),
    )
    .unwrap();
    sim.start(None).unwrap();
    let failure: sim_core::Failure = Arc::new(std::io::Error::other("batch observer failed"));
    let returned = failure.clone();
    sim.set_observer(Some(Rc::new(move |event, _| {
        if event.kind == "referee.batch.admitted" {
            Err(returned.clone())
        } else {
            Ok(())
        }
    })));
    sim.schedule_input(
        ep("referee.enable"),
        HostInput::RefereeEnable(true),
        Some(0),
    )
    .unwrap();
    assert!(Arc::ptr_eq(&failure, &sim.run_until(10, None).unwrap_err()));
    let handle = sim.handle::<RefereeDevice>("referee").unwrap();
    let owner = sim.component(&handle).unwrap();
    assert_eq!((owner.publications(), owner.next_sequence()), (1, 4));
    sim.close().unwrap();
}
