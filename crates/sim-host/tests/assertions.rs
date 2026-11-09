use sim_host::{assertions::*, can::CanFrame};

fn assertion(before_ns: u64, endpoint: &str, condition: Condition) -> Assertion {
    Assertion {
        before_ns,
        endpoint: endpoint.into(),
        condition,
    }
}
fn uart<'a>(timestamp_ns: u64, endpoint: &'a str, bytes: &'a [u8]) -> Observation<'a> {
    Observation {
        endpoint,
        timestamp_ns,
        data: ObservationData::Uart(bytes),
    }
}

#[test]
fn uart_matches_split_utf8_and_nul_strictly_before_deadline() {
    for (timestamp, expected) in [(149, Status::Pass), (150, Status::Fail)] {
        let mut evaluator = Evaluator::new(
            vec![assertion(
                50,
                "serial.rx",
                Condition::UartContains("é\0OK".as_bytes().into()),
            )],
            100,
            100,
        )
        .unwrap();
        evaluator
            .observe(&[], &[uart(110, "serial.rx", &[0xc3])], 110, true)
            .unwrap();
        evaluator
            .observe(&[], &[uart(timestamp, "serial.rx", b"\xa9\0OK")], 150, true)
            .unwrap();
        assert_eq!(evaluator.status(), [expected]);
        // A previous failed deadline cannot be rescued by old wire metadata.
        evaluator
            .observe(
                &[],
                &[uart(120, "serial.rx", "é\0OK".as_bytes())],
                151,
                true,
            )
            .unwrap();
        assert_eq!(evaluator.status(), [expected]);
    }
    let mut evaluator = Evaluator::new(
        vec![assertion(
            10,
            "serial.rx",
            Condition::UartContains(b"X".to_vec()),
        )],
        10,
        0,
    )
    .unwrap();
    evaluator
        .observe(&[], &[uart(1, "serial.rx", b"X")], 1, true)
        .unwrap();
    assert!(evaluator.all_passed()); // one-byte patterns retain no tail
}

#[test]
fn complete_batch_admission_precedes_effects_and_same_cut_matching_precedes_expiry() {
    let mut evaluator = Evaluator::new(
        vec![assertion(
            10,
            "serial.rx",
            Condition::UartContains(b"AB".to_vec()),
        )],
        20,
        100,
    )
    .unwrap();
    assert!(evaluator
        .observe(
            &[],
            &[uart(105, "serial.rx", b"AB"), uart(111, "serial.rx", b"!")],
            110,
            true
        )
        .is_err());
    assert_eq!(evaluator.status(), [Status::Pending]);
    evaluator
        .observe(&[], &[uart(109, "serial.rx", b"A")], 110, false)
        .unwrap();
    evaluator
        .observe(&[], &[uart(109, "serial.rx", b"B")], 110, false)
        .unwrap();
    evaluator.advance(110).unwrap();
    assert_eq!(evaluator.status(), [Status::Pass]);
    assert!(evaluator.advance(109).is_err());
    assert!(evaluator.finish(119).is_err());
    assert_eq!(evaluator.finish(120).unwrap(), [Status::Pass]);
    assert_eq!(evaluator.finish(120).unwrap(), [Status::Pass]);
    assert!(evaluator.observe(&[], &[], 120, true).is_err());
    assert!(Evaluator::new(vec![], 1, u64::MAX).is_err());
}

#[test]
fn can_uses_wire_time_and_optional_fields_digital_uses_completed_bound() {
    let wanted = CanMatch {
        can_id: 0x123,
        flags: None,
        dlc: None,
        data: Some(vec![0x80]),
    };
    let mut evaluator = Evaluator::new(
        vec![
            assertion(10, "bus.frames", Condition::CanSeen(wanted)),
            assertion(10, "key.level", Condition::DigitalEquals(true)),
            assertion(10, "serial.rx", Condition::UartContains(b"no".to_vec())),
        ],
        20,
        100,
    )
    .unwrap();
    let frame = CanFrame::new(0x123, 0, None, vec![0x80], 999).unwrap();
    let event = |timestamp| Observation {
        endpoint: "bus.frames",
        timestamp_ns: timestamp,
        data: ObservationData::Can(&frame),
    };
    evaluator.observe(&[], &[event(99)], 100, true).unwrap();
    assert_eq!(evaluator.status(), [Status::Pending; 3]);
    evaluator
        .observe(&[("key.level", true)], &[event(109)], 110, true)
        .unwrap();
    assert_eq!(
        evaluator.status(),
        [Status::Pass, Status::Fail, Status::Fail]
    );
    assert_eq!(
        evaluator.failures(),
        [
            "assertion[2] digital_equals key.level did not match before 10 ns",
            "assertion[3] uart_contains serial.rx did not match before 10 ns"
        ]
    );
    assert_eq!(
        evaluator.finish(120).unwrap(),
        [Status::Pass, Status::Fail, Status::Fail]
    );
    let invalid = CanMatch {
        can_id: 0x800,
        flags: Some(0),
        dlc: None,
        data: None,
    };
    assert!(Evaluator::new(
        vec![assertion(1, "bus.frames", Condition::CanSeen(invalid))],
        1,
        0
    )
    .is_err());
}
