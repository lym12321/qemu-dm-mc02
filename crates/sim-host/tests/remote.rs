#![cfg(all(target_os = "linux", feature = "machine"))]
use sim_core::{Direction, Endpoint, EventPayload, NodeSpec, SimulationOptions, Topology};
use sim_host::{
    machine::{QemuMachine, QemuOptions},
    profile::{self, MachineInput},
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
};
use std::{path::PathBuf, time::Duration};

fn node(options: QemuOptions) -> NodeSpec<HostOptions> {
    NodeSpec::new("board", "machine.qemu", HostOptions::Qemu(options))
}
#[test]
fn remote_is_an_explicit_raw_input_and_uart5_is_reserved_before_io() {
    let mut options = QemuOptions::new("not-opened.elf");
    assert!(profile::describe(&node(options.clone()))
        .unwrap()
        .port("remote")
        .is_err());
    options.remote = true;
    let owned = tempfile::tempdir().unwrap();
    let socket = owned.path().join("already-owned-qmp");
    std::fs::write(&socket, []).unwrap();
    options.qmp_path = Some(socket);
    let spec = node(options.clone());
    let description = profile::describe(&spec).unwrap();
    let remote = description.port("remote").unwrap();
    assert_eq!(
        (&*remote.protocol, remote.direction),
        ("remote", Direction::In)
    );
    assert!(description.port("uart5").is_err());
    for bytes in [vec![], vec![0, 127, 128, 255], vec![0; 4096]] {
        assert!(profile::validate_input(
            &spec,
            "remote",
            HostInput::Machine(MachineInput::Bytes(bytes)),
            &[]
        )
        .is_ok());
    }
    for input in [MachineInput::Bytes(vec![0; 4097]), MachineInput::Bool(true)] {
        assert!(profile::validate_input(&spec, "remote", HostInput::Machine(input), &[]).is_err());
    }
    options.uarts.push("uart5".into());
    assert!(profile::describe(&node(options)).is_err());
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_REMOTE_ELF"]
fn native_remote_bytes_are_read_from_uart5_and_reported_by_uart1() {
    let mut options = QemuOptions::new(PathBuf::from(
        std::env::var_os("SIM_NATIVE_REMOTE_ELF").unwrap(),
    ));
    options.qemu = Some(PathBuf::from(std::env::var_os("SIM_NATIVE_QEMU").unwrap()));
    options.remote = true;
    let directory = tempfile::tempdir().unwrap();
    let qmp = directory.path().join("external-qmp.sock");
    options.qmp_path = Some(qmp.clone());
    let mut sim = HostSimulation::new(
        Topology::new(vec![node(options)], vec![]).unwrap(),
        runtime::registry().unwrap(),
        SimulationOptions {
            host_timeout: Some(Duration::from_secs(10)),
            quantum_ns: 1_000_000,
            ..Default::default()
        },
    )
    .unwrap();
    sim.start(None).unwrap();
    let handle = sim.handle::<QemuMachine>("board").unwrap();
    let owner = sim.component(&handle).unwrap().connection_info().unwrap();
    let result = (|| {
        let mut output = vec![];
        for time in 1..=8 {
            sim.run_until(time * 1_000_000, None)?;
            output = uart(&sim);
            if output.windows(13).any(|v| v == b"REMOTE_READY\n") {
                break;
            }
        }
        assert!(output.windows(13).any(|v| v == b"REMOTE_READY\n"));
        assert!(qmp.exists());
        sim.schedule_input(
            Endpoint::parse("board.key").unwrap(),
            HostInput::Machine(MachineInput::Bool(true)),
            None,
        )?;
        sim.schedule_input(
            Endpoint::parse("board.remote").unwrap(),
            HostInput::Machine(MachineInput::Bytes(vec![0, 127, 128, 255])),
            None,
        )?;
        sim.run_until(20_000_000, None)?;
        let output = uart(&sim);
        assert!(
            output
                .windows(16)
                .any(|v| v == b"REMOTE_READ:\0\x7f\x80\xff"),
            "{output:?}"
        );
        assert!(
            output.windows(12).any(|v| v == b"REMOTE_PASS\n"),
            "{output:?}"
        );
        assert!(!sim
            .events()
            .iter()
            .any(|event| event.kind == "uart" && event.source_port.as_deref() == Some("remote")));
        Ok::<_, sim_core::Failure>(())
    })();
    let cleanup = sim.close();
    assert!(!PathBuf::from(format!("/proc/{}", owner.pid)).exists());
    assert!(!owner.directory.unwrap().exists());
    result.unwrap();
    cleanup.unwrap();
}
fn uart(sim: &HostSimulation) -> Vec<u8> {
    sim.events()
        .iter()
        .filter_map(|event| match &event.payload {
            EventPayload::Observation(HostEvent::MachineUart { data }) => Some(data.clone()),
            _ => None,
        })
        .flatten()
        .collect()
}
