#![cfg(feature = "scene")]
use sim_host::apps::presets::{add_can_faults, four_wheel_document, CanFault, FourWheelOptions};
use std::collections::BTreeMap;

fn options() -> FourWheelOptions {
    FourWheelOptions {
        firmwares: vec![
            ("east".into(), "/admission/east.elf".into()),
            ("west".into(), "/admission/west.elf".into()),
        ],
        model: "/admission/world.xml".into(),
        ready: [
            ("east".into(), "FIRST".into()),
            ("west".into(), "SECOND".into()),
        ]
        .into(),
        referees: BTreeMap::new(),
        duration_ns: 50_000_000,
        quantum_ns: 1_000_000,
        imus: true,
        inputs: vec![],
        assertions: vec![],
    }
}

#[test]
fn preserves_python_wiring_and_fault_order() {
    let mut document = four_wheel_document(options()).unwrap();
    // Equal-time feedback is intentionally supplied before delay; the public
    // contract orders delay first, retaining the two distinct fault effects.
    add_can_faults(
        &mut document,
        "east",
        vec![
            (0, CanFault::Delay(5_000_000)),
            (
                25_000_000,
                CanFault::Feedback {
                    motor_id: 1,
                    enabled: false,
                },
            ),
            (25_000_000, CanFault::Delay(0)),
            (
                35_000_000,
                CanFault::Feedback {
                    motor_id: 1,
                    enabled: true,
                },
            ),
        ],
    )
    .unwrap();
    let expected: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python_four_wheel_document.json")).unwrap();
    assert_eq!(document, expected);
    #[cfg(all(
        target_os = "linux",
        feature = "machine",
        feature = "devices",
        feature = "mujoco"
    ))]
    {
        use sim_host::config::{scene::parse_scene, yaml::parse_document};
        let directory = tempfile::tempdir().unwrap();
        let model = directory.path().join("world.xml");
        std::fs::write(&model, "<mujoco/>").unwrap();
        document["nodes"]["world"]["model"] = serde_json::json!(model);
        let scene = parse_scene(
            std::path::PathBuf::from("/admission/preset.json"),
            &parse_document(&serde_json::to_vec(&document).unwrap()).unwrap(),
        )
        .unwrap();
        let simulation = scene.simulation(Default::default()).unwrap();
        assert_eq!(simulation.state(), sim_core::SimulationState::New);
    }
}

#[test]
fn invalid_changes_leave_the_input_document_unchanged() {
    let mut document = four_wheel_document(options()).unwrap();
    let original = document.clone();
    for changes in [
        vec![(
            0,
            CanFault::Feedback {
                motor_id: 5,
                enabled: false,
            },
        )],
        (0..17).map(|i| (i, CanFault::Delay(i))).collect(),
    ] {
        assert!(add_can_faults(&mut document, "east", changes).is_err());
        assert_eq!(document, original);
    }
    let mut invalid = options();
    invalid.ready.insert("undeclared".into(), "READY".into());
    assert!(four_wheel_document(invalid).is_err());
}
