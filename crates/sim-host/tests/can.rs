//! Independent expectations from the existing public host CAN contract and
//! fixed84 literals in tests/test_can_fabric.py; no electrical timing claim.

use sim_host::{
    can::*,
    transports::fixed84::{decode, encode},
    Error, Failure, Result,
};
use std::sync::Arc;

fn frame(id: u32, time: u64) -> CanFrame {
    CanFrame::new(id, 0, None, vec![], time).unwrap()
}

#[derive(Default)]
struct Sink {
    received: Vec<(EndpointId, CanFrame)>,
    observed: Vec<Observation>,
}

type Observation = (String, u32, u64, u64, u64, u64, bool, bool);

impl CanCallbacks for Sink {
    fn observe(&mut self, item: &CanDelivery<'_>, _: &mut CanBus) -> Result<()> {
        self.observed.push((
            item.destination_name.to_owned(),
            item.frame.can_id(),
            item.sequence,
            item.frame.timestamp_ns(),
            item.due_ns,
            item.delivered_ns,
            item.late,
            item.dropped,
        ));
        Ok(())
    }
    fn receive(
        &mut self,
        destination: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> Result<()> {
        self.received.push((destination.clone(), frame.clone()));
        Ok(())
    }
}

fn attach(bus: &mut CanBus, name: &str) -> EndpointId {
    let id = EndpointId::new();
    bus.attach(&id, name).unwrap();
    id
}

#[test]
fn frame_admission_retains_explicit_dlc_and_rejects_unrepresentable_values() {
    for (dlc, length) in [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]
        .into_iter()
        .enumerate()
    {
        assert_eq!(length_to_dlc(length, true).unwrap(), dlc as u8);
        assert_eq!(dlc_to_length(dlc as u8, true).unwrap(), length);
    }
    for (id, flags, dlc, length) in [
        (0x800, 0, None, 0),
        (0x2000_0000, EXTENDED, None, 0),
        (1, 16, None, 0),
        (1, FD | RTR, None, 0),
        (1, BRS, None, 0),
        (1, 0, Some(9), 0),
        (1, FD, Some(16), 0),
        (1, FD, None, 9),
        (1, FD, None, 65),
        (1, 0, Some(1), 2),
    ] {
        assert!(CanFrame::new(id, flags, dlc, vec![0; length], 0).is_err());
    }
    assert!(length_to_dlc(9, false).is_err());
    assert!(dlc_to_length(9, false).is_err());
    let padded = CanFrame::new(
        0x1fff_ffff,
        EXTENDED | FD | BRS,
        Some(15),
        vec![1, 2],
        u64::MAX,
    )
    .unwrap();
    assert_eq!((padded.length(), padded.data()), (64, &[1, 2][..]));
    // Existing RTR admission permits an explicit DLC and data; do not add a
    // new Linux-frame policy to the protocol-neutral host frame.
    assert!(CanFrame::new(1, RTR, Some(8), vec![3], 0).is_ok());
}

fn literal(header: &[u8], payload: &[u8]) -> Vec<u8> {
    let mut bytes = header.to_vec();
    bytes.extend_from_slice(payload);
    bytes.resize(84, 0);
    bytes
}

#[test]
fn fixed84_matches_independent_classic_and_fd_literals() {
    let classic = CanFrame::new(0x123, 0, None, b"12345678".to_vec(), 17).unwrap();
    let classic_wire = literal(
        &[
            0x23, 1, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 17, 0, 0, 0, 0, 0, 0, 0,
        ],
        b"12345678",
    );
    let fd = CanFrame::new(
        0x1abcde,
        EXTENDED | FD | BRS,
        None,
        (0..12).collect::<Vec<u8>>(),
        9,
    )
    .unwrap();
    let fd_wire = literal(
        &[
            0xde, 0xbc, 0x1a, 0, 13, 0, 0, 0, 9, 0, 0, 0, 9, 0, 0, 0, 0, 0, 0, 0,
        ],
        &(0..12).collect::<Vec<_>>(),
    );
    for (frame, wire) in [(classic, classic_wire), (fd, fd_wire)] {
        assert_eq!(encode(&frame).as_slice(), wire);
        assert_eq!(decode(&wire).unwrap(), frame);
    }
}

#[test]
fn fixed84_rejects_invalid_boundaries_and_ignores_unused_tail_bytes() {
    let raw = encode(&frame(1, 0));
    for length in [0, 19, 83, 85] {
        assert!(decode(&vec![0; length]).is_err());
    }
    for (offset, value) in [(9, 1), (10, 1), (11, 1), (4, 16), (1, 8), (8, 9)] {
        let mut bad = raw;
        bad[offset] = value;
        assert!(decode(&bad).is_err());
    }
    let mut ignored = raw;
    ignored[83] = 0xff;
    assert_eq!(decode(&ignored).unwrap(), frame(1, 0));
}

#[test]
fn bus_orders_due_times_ties_and_destinations_with_original_observations() {
    let mut bus = CanBus::new(CanBusOptions {
        delay_ns: 3,
        ..Default::default()
    })
    .unwrap();
    let source = attach(&mut bus, "source");
    let first = attach(&mut bus, "first");
    let second = attach(&mut bus, "second");
    let mut sink = Sink::default();
    for (id, time) in [(0x103, 7), (0x101, 2), (0x102, 2)] {
        bus.send(&source, frame(id, time), None).unwrap();
    }
    assert_eq!(bus.next_due_ns().unwrap(), Some(5));
    assert!(!bus.has_due(4).unwrap());
    assert_eq!(bus.step(4, None, &mut sink).unwrap(), 0);
    assert_eq!(bus.step(5, None, &mut sink).unwrap(), 2);
    assert_eq!(
        sink.received,
        vec![
            (first.clone(), frame(0x101, 5)),
            (second.clone(), frame(0x101, 5)),
            (first.clone(), frame(0x102, 5)),
            (second.clone(), frame(0x102, 5))
        ]
    );
    bus.step(11, None, &mut sink).unwrap();
    assert_eq!(
        &sink.observed[4..],
        &[
            ("first".to_owned(), 0x103, 0, 7, 10, 11, true, false),
            ("second".to_owned(), 0x103, 0, 7, 10, 11, true, false)
        ]
    );
    assert_eq!(bus.pending(), 0);
}

#[test]
fn delay_changes_do_not_rewrite_pending_deadlines_and_floor_precedes_delay() {
    let mut bus = CanBus::new(CanBusOptions {
        delay_ns: 10,
        ..Default::default()
    })
    .unwrap();
    let source = attach(&mut bus, "source");
    attach(&mut bus, "sink");
    bus.send(&source, frame(1, 5), None).unwrap();
    bus.set_delay_ns(0).unwrap();
    bus.send(&source, frame(2, 6), None).unwrap();
    bus.send(&source, frame(3, 15), None).unwrap();
    bus.set_delay_ns(3).unwrap();
    bus.send(&source, frame(4, 1), Some(20)).unwrap();
    let mut sink = Sink::default();
    bus.step(6, None, &mut sink).unwrap();
    bus.step(15, None, &mut sink).unwrap();
    assert_eq!(bus.next_due_ns().unwrap(), Some(23));
    bus.step(23, None, &mut sink).unwrap();
    assert_eq!(
        sink.observed
            .iter()
            .map(|item| (item.1, item.2, item.3, item.4))
            .collect::<Vec<_>>(),
        vec![(2, 1, 6, 6), (1, 0, 5, 15), (3, 2, 15, 15), (4, 3, 1, 23)]
    );
}

#[test]
fn identity_and_time_admission_are_atomic_and_do_not_freeze_wiring() {
    assert!(CanBus::new(CanBusOptions {
        max_pending: 0,
        ..Default::default()
    })
    .is_err());
    assert!(CanBus::new(CanBusOptions {
        dispatch_budget: 0,
        ..Default::default()
    })
    .is_err());
    let mut bus = CanBus::new(CanBusOptions {
        delay_ns: 1,
        ..Default::default()
    })
    .unwrap();
    let source = attach(&mut bus, "source");
    assert!(bus.attach(&source, "same identity").is_err());
    assert!(bus.attach(&EndpointId::new(), "source").is_err());
    assert!(bus.send(&EndpointId::new(), frame(1, 0), None).is_err());
    assert!(bus.send(&source, frame(1, u64::MAX), None).is_err());
    assert!(bus.failure().is_none());
    assert_eq!(bus.pending(), 0);
    attach(&mut bus, "still admissible");
    bus.send(&source, frame(1, 0), None).unwrap();
    assert!(bus.attach(&EndpointId::new(), "late").is_err());
    bus.step(10, Some(0), &mut Sink::default()).unwrap();
    assert!(bus.step(9, None, &mut Sink::default()).is_err());
    assert_eq!(bus.time_ns(), 10);
    assert!(bus.failure().is_none());
}

#[test]
fn recursive_emits_share_one_queue_and_both_dispatch_budgets() {
    struct Echo {
        source: EndpointId,
        received: Vec<u32>,
    }
    impl CanCallbacks for Echo {
        fn receive(&mut self, _: &EndpointId, value: &CanFrame, bus: &mut CanBus) -> Result<()> {
            self.received.push(value.can_id());
            bus.send(
                &self.source,
                frame(value.can_id() + 1, value.timestamp_ns()),
                None,
            )
        }
    }
    let mut bus = CanBus::new(CanBusOptions {
        loopback: true,
        dispatch_budget: 3,
        ..Default::default()
    })
    .unwrap();
    let source = attach(&mut bus, "echo");
    let mut echo = Echo {
        source: source.clone(),
        received: vec![],
    };
    bus.send(&source, frame(1, 0), None).unwrap();
    assert_eq!(bus.step(0, Some(1), &mut echo).unwrap(), 1);
    assert_eq!(bus.step(0, None, &mut echo).unwrap(), 3);
    assert_eq!(echo.received, vec![1, 2, 3, 4]);
    assert_eq!((bus.pending(), bus.next_due_ns().unwrap()), (1, Some(0)));
}

#[test]
fn capacity_freezes_retains_first_error_and_reset_discards_queued_work() {
    let mut bus = CanBus::new(CanBusOptions {
        max_pending: 1,
        delay_ns: 3,
        ..Default::default()
    })
    .unwrap();
    let source = attach(&mut bus, "source");
    attach(&mut bus, "sink");
    bus.send(&source, frame(1, 100), None).unwrap();
    let primary = bus.send(&source, frame(2, 0), None).unwrap_err();
    assert!(matches!(&*primary, Error::Capacity(_)));
    assert!(Arc::ptr_eq(
        &primary,
        &bus.step(100, None, &mut Sink::default()).unwrap_err()
    ));
    assert_eq!((bus.pending(), bus.time_ns()), (1, 0));
    bus.reset(5).unwrap();
    assert_eq!((bus.pending(), bus.delay_ns()), (0, 3));
    assert!(bus.attach(&EndpointId::new(), "still locked").is_err());
    bus.send(&source, frame(3, 2), Some(10)).unwrap();
    let mut sink = Sink::default();
    bus.step(13, None, &mut sink).unwrap();
    assert_eq!(sink.observed[0].2, 0);
    assert_eq!(sink.received[0].1, frame(3, 13));
    bus.close().unwrap();
    bus.close().unwrap();
    assert_eq!(bus.next_due_ns().unwrap(), None);
    assert!(bus.send(&source, frame(4, 13), None).is_err());
}

#[test]
fn callbacks_fail_in_order_without_replaying_partial_fanout() {
    struct Failing {
        phase: usize,
        primary: Failure,
        calls: Vec<&'static str>,
    }
    impl CanCallbacks for Failing {
        fn should_drop(&mut self, item: &CanDelivery<'_>, _: &mut CanBus) -> Result<bool> {
            self.calls.push("drop");
            if self.phase == 0 && item.destination_name == "second" {
                return Err(self.primary.clone());
            }
            Ok(item.destination_name == "discard")
        }
        fn observe(&mut self, item: &CanDelivery<'_>, _: &mut CanBus) -> Result<()> {
            self.calls.push("log");
            if self.phase == 1 && item.destination_name == "second" {
                return Err(self.primary.clone());
            }
            Ok(())
        }
        fn receive(&mut self, _: &EndpointId, _: &CanFrame, _: &mut CanBus) -> Result<()> {
            self.calls.push("receive");
            if self.phase == 2 && self.calls.iter().filter(|&&call| call == "receive").count() == 2
            {
                return Err(self.primary.clone());
            }
            Ok(())
        }
    }
    for phase in 0..3 {
        let mut bus = CanBus::new(Default::default()).unwrap();
        let source = attach(&mut bus, "source");
        for name in ["first", "discard", "second", "never"] {
            attach(&mut bus, name);
        }
        let mut sink = Failing {
            phase,
            primary: Arc::new(Error::Callback("primary".into())),
            calls: vec![],
        };
        bus.send(&source, frame(1, 0), None).unwrap();
        bus.send(&source, frame(2, 0), None).unwrap();
        let returned = bus.step(0, None, &mut sink).unwrap_err();
        assert!(Arc::ptr_eq(&sink.primary, &returned));
        let calls = sink.calls.clone();
        assert_eq!(&calls[..5], &["drop", "log", "receive", "drop", "log"]);
        assert_eq!(calls.len(), 6 + phase);
        assert_eq!(bus.pending(), 1);
        let repeated = bus.step(0, None, &mut sink).unwrap_err();
        assert!(Arc::ptr_eq(&sink.primary, &repeated));
        assert_eq!(sink.calls, calls);
    }
}

#[test]
fn swallowed_capacity_failure_stops_each_callback_phase_and_keeps_primary() {
    struct Exhaust {
        source: EndpointId,
        phase: usize,
        primary: Option<Failure>,
        received: usize,
    }
    impl Exhaust {
        fn exhaust(&mut self, bus: &mut CanBus) -> Result<()> {
            bus.send(&self.source, frame(2, 0), None)?;
            self.primary = Some(bus.send(&self.source, frame(3, 0), None).unwrap_err());
            Ok(()) // Deliberately swallow; the bus must check its own latch.
        }
    }
    impl CanCallbacks for Exhaust {
        fn should_drop(&mut self, _: &CanDelivery<'_>, bus: &mut CanBus) -> Result<bool> {
            if self.phase == 0 {
                self.exhaust(bus)?;
            }
            Ok(false)
        }
        fn observe(&mut self, _: &CanDelivery<'_>, bus: &mut CanBus) -> Result<()> {
            if self.phase == 1 {
                self.exhaust(bus)?;
            }
            Ok(())
        }
        fn receive(&mut self, _: &EndpointId, _: &CanFrame, bus: &mut CanBus) -> Result<()> {
            self.received += 1;
            self.exhaust(bus)?;
            if self.phase == 3 {
                return Err(Arc::new(Error::Callback("replacement".into())));
            }
            Ok(())
        }
    }
    for phase in 0..4 {
        let mut bus = CanBus::new(CanBusOptions {
            max_pending: 1,
            ..Default::default()
        })
        .unwrap();
        let source = attach(&mut bus, "source");
        attach(&mut bus, "first");
        attach(&mut bus, "never");
        bus.send(&source, frame(1, 0), None).unwrap();
        let mut sink = Exhaust {
            source,
            phase,
            primary: None,
            received: 0,
        };
        let returned = bus.step(0, None, &mut sink).unwrap_err();
        assert!(Arc::ptr_eq(sink.primary.as_ref().unwrap(), &returned));
        assert_eq!(sink.received, usize::from(phase >= 2));
        assert_eq!(bus.pending(), 1);
    }
}

#[test]
fn callback_lifecycle_mutation_is_rejected_before_changes() {
    struct Mutate(usize);
    impl CanCallbacks for Mutate {
        fn receive(&mut self, _: &EndpointId, _: &CanFrame, bus: &mut CanBus) -> Result<()> {
            match self.0 {
                0 => bus.step(0, None, &mut Sink::default()).map(|_| ()),
                1 => bus.reset(100),
                2 => bus.close(),
                _ => bus.set_delay_ns(100),
            }
        }
    }
    for operation in 0..4 {
        let mut bus = CanBus::new(Default::default()).unwrap();
        let source = attach(&mut bus, "source");
        attach(&mut bus, "sink");
        bus.send(&source, frame(1, 0), None).unwrap();
        bus.send(&source, frame(2, 0), None).unwrap();
        assert!(bus.step(0, None, &mut Mutate(operation)).is_err());
        assert_eq!((bus.pending(), bus.time_ns(), bus.delay_ns()), (1, 0, 0));
        bus.reset(5).unwrap();
        assert_eq!(bus.pending(), 0);
    }
}
