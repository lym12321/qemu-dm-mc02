#![cfg(all(feature = "scene", feature = "gazebo"))]

use sim_core::SimulationState;
use sim_host::{
    config::{scene::parse_scene, yaml::parse_document},
    physics::factory::WorldControl,
    runtime::{HostInput, HostOptions},
};

const SCENE: &str = "schema: sim/v1\nruntime: {duration: 10ms, quantum: 2ms}\nnodes:\n  world:\n    kind: physics.gazebo\n    model: rotor.sdf\n    resource_paths: [assets]\n    joints: {shaft: [rotor, shaft]}\n    bodies: {body: [rotor, body]}\n    imus: {inertial: [rotor, body, imu]}\nexperiment:\n  inputs: [{at: 0ns, endpoint: world.effort, value: {shaft: 0.4}}]\n";

#[test]
fn gazebo_scene_admits_relative_resources_and_effort_without_parsing_the_native_model() {
    let directory = tempfile::tempdir().unwrap();
    let model = directory.path().join("rotor.sdf");
    std::fs::write(&model, "invalid SDF must not be acquired during admission").unwrap();
    std::fs::create_dir(directory.path().join("assets")).unwrap();
    let path = directory.path().join("scene.yaml");
    let scene = parse_scene(path, &parse_document(SCENE.as_bytes()).unwrap()).unwrap();
    let HostOptions::Gazebo(options) = &scene.topology.nodes[0].options else {
        panic!("Gazebo typed options")
    };
    assert_eq!(options.model, model);
    assert_eq!(options.resource_paths, [directory.path().join("assets")]);
    assert_eq!(options.control, WorldControl::Direct);
    assert_eq!(
        options.bindings.joints,
        [("shaft".into(), "rotor".into(), "shaft".into())]
    );
    assert_eq!(
        options.bindings.bodies,
        [("body".into(), "rotor".into(), "body".into())]
    );
    assert_eq!(
        options.bindings.imus,
        [(
            "inertial".into(),
            "rotor".into(),
            "body".into(),
            "imu".into()
        )]
    );
    assert_eq!(
        scene.experiment.as_ref().unwrap().inputs[0].payload,
        HostInput::JointEffort(vec![("shaft".into(), 0.4)])
    );
    assert_eq!(
        scene.simulation(Default::default()).unwrap().state(),
        SimulationState::New
    );

    for (from, to) in [
        ("[rotor, shaft]", "[rotor]"),
        ("[rotor, body, imu]", "[rotor, body]"),
        (
            "{shaft: [rotor, shaft]}",
            "{shaft: [rotor, shaft], alias: [rotor, shaft]}",
        ),
        ("imus: {inertial:", "imus: {nested.port:"),
        ("resource_paths: [assets]", "resource_paths: [rotor.sdf]"),
        (
            "kind: physics.gazebo",
            "kind: physics.gazebo\n    cameras: {}",
        ),
        ("shaft: 0.4", "shaft: '0.4'"),
        ("shaft: 0.4", "shaft: .nan"),
    ] {
        let invalid = SCENE.replace(from, to);
        let result = parse_scene(
            directory.path().join("scene.yaml"),
            &parse_document(invalid.as_bytes()).unwrap(),
        );
        assert!(result.is_err(), "unexpected admission: {invalid}");
    }
}
