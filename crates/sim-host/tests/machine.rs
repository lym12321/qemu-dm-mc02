#![cfg(all(target_os = "linux", feature = "machine"))]
use serde_json::json;
use sim_core::{
    Endpoint, EventPayload, LinkSpec, NodeSpec, Session, SimulationOptions, SimulationState,
    TimeQuality, Topology,
};
use sim_host::{
    can::{CanBusOptions, CanFrame},
    fabric::digital::DigitalNetOptions,
    machine::{QemuMachine, QemuOptions},
    profile::{self, MachineInput},
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}
fn link(source: &str, target: &str) -> LinkSpec {
    LinkSpec::new(ep(source), ep(target))
}
fn machine(name: &str, firmware: &Path, qemu: &Path) -> NodeSpec<HostOptions> {
    let mut options = QemuOptions::new(firmware);
    options.qemu = Some(qemu.to_owned());
    NodeSpec::new(name, "machine.qemu", HostOptions::Qemu(options))
}
fn fixture(mode: &str) -> (tempfile::TempDir, PathBuf, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let firmware = dir.path().join("fixture.elf");
    fs::write(&firmware, mode).unwrap();
    let qemu = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/support/machine_qemu.py");
    (dir, firmware, qemu)
}
fn simulation(nodes: Vec<NodeSpec<HostOptions>>, links: Vec<LinkSpec>) -> HostSimulation {
    HostSimulation::new(
        Topology::new(nodes, links).unwrap(),
        runtime::registry().unwrap(),
        SimulationOptions {
            initial_session: Session::new(1, 4_000).unwrap(),
            host_timeout: Some(Duration::from_secs(10)),
            ..Default::default()
        },
    )
    .unwrap()
}
fn info(sim: &HostSimulation, name: &str) -> sim_host::machine::ConnectionInfo {
    sim.component(&sim.handle::<QemuMachine>(name).unwrap())
        .unwrap()
        .connection_info()
        .unwrap()
}
fn clean(info: &sim_host::machine::ConnectionInfo) {
    assert!(!PathBuf::from(format!("/proc/{}", info.pid)).exists());
    assert!(!info.directory.as_ref().unwrap().exists());
}

#[test]
fn pure_profile_rejects_bad_pins_input_aliases_and_configuration_before_io() {
    let (_, firmware, qemu) = fixture("good");
    let node = machine("m", &firmware, &qemu);
    assert!(profile::describe(&node).is_ok());
    for pin in ["A00", "A16", "I0", "A-1", "é1", "", "A1.out"] {
        assert!(profile::gpio_pin(pin).is_err(), "{pin}");
    }
    let input = HostInput::Machine(MachineInput::Bool(true));
    assert!(
        profile::validate_input(&node, "key", input.clone(), &["gpio.A15.out".into()]).is_err()
    );
    assert!(profile::validate_input(&node, "key", input, &[]).is_ok());
    assert!(profile::validate_input(
        &node,
        "uart1",
        HostInput::Machine(MachineInput::Bytes(vec![0; 4097])),
        &[]
    )
    .is_err());
    let nodes = vec![
        node,
        NodeSpec::new(
            "net",
            "net.digital",
            HostOptions::DigitalNet(DigitalNetOptions::default()),
        ),
    ];
    assert!(HostSimulation::new(
        Topology::new(
            nodes,
            vec![
                link("m.gpio.A15.out", "net.signal"),
                link("m.key", "net.signal")
            ]
        )
        .unwrap(),
        runtime::registry().unwrap(),
        SimulationOptions::default()
    )
    .is_err());
}

#[test]
fn two_actual_children_receive_every_request_before_any_completion_and_map_uart_after_read() {
    let (_dir, firmware, qemu) = fixture("barrier");
    let mut sim = simulation(
        vec![
            machine("a", &firmware, &qemu),
            machine("b", &firmware, &qemu),
        ],
        vec![],
    );
    sim.start(None).unwrap();
    let owners = [info(&sim, "a"), info(&sim, "b")];
    let result = (|| {
        sim.run_until(5_000, None)?;
        Ok::<_, sim_core::Failure>(())
    })();
    // The fixture records one native request per real child; the first cannot
    // report reached until both requests exist.
    assert!(result.is_ok(), "{result:?}");
    assert_eq!(
        fs::read_to_string(firmware.with_extension("requests"))
            .unwrap()
            .lines()
            .count(),
        2
    );
    let events: Vec<_> = sim
        .events()
        .into_iter()
        .filter(|event| {
            matches!(
                &event.payload,
                EventPayload::Observation(HostEvent::MachineUart { .. })
            )
        })
        .collect();
    assert_eq!(events.len(), 2);
    for event in events {
        assert_eq!(
            (
                event.source_time_ns,
                event.mapped_time_ns,
                event.delivery_time_ns,
                event.quality
            ),
            (Some(1900), 5000, 5000, TimeQuality::UpperBound)
        );
        assert_eq!(
            event.payload,
            EventPayload::Observation(HostEvent::MachineUart {
                data: b"actual-peer\0UART".to_vec()
            })
        );
    }
    sim.close().unwrap();
    for owner in &owners {
        clean(owner);
    }
}

#[test]
fn interrupted_or_wrong_target_freezes_last_committed_time_and_cleanup_still_reaps() {
    for mode in ["interrupted", "wrong-time"] {
        let (_dir, firmware, qemu) = fixture(mode);
        let mut sim = simulation(vec![machine("m", &firmware, &qemu)], vec![]);
        sim.start(None).unwrap();
        let owner = info(&sim, "m");
        let error = sim.run_until(5_000, None).unwrap_err();
        assert_eq!(
            (sim.state(), sim.t_sim_ns()),
            (SimulationState::Failed, 4000)
        );
        assert!(Arc::ptr_eq(
            &error,
            &sim.schedule_input(
                ep("m.key"),
                HostInput::Machine(MachineInput::Bool(true)),
                None
            )
            .unwrap_err()
        ));
        let _ = sim.close();
        clean(&owner);
    }
}

#[test]
fn actual_paused_child_exit_is_terminal_before_an_independent_borrowed_io() {
    let (_dir, firmware, qemu) = fixture("good");
    let mut sim = simulation(
        vec![
            machine("m", &firmware, &qemu),
            NodeSpec::new(
                "bus",
                "bus.can",
                HostOptions::CanBus(CanBusOptions::default().into()),
            ),
            NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
        ],
        vec![link("host.can", "bus.can")],
    );
    sim.start(None).unwrap();
    let owner = info(&sim, "m");
    let pid = rustix::process::Pid::from_raw(owner.pid as i32).unwrap();
    let pidfd = rustix::process::pidfd_open(pid, rustix::process::PidfdFlags::empty()).unwrap();
    rustix::process::kill_process(pid, rustix::process::Signal::TERM).unwrap();
    let mut fd = [rustix::event::PollFd::new(
        &pidfd,
        rustix::event::PollFlags::IN,
    )];
    rustix::event::poll(
        &mut fd,
        Some(&rustix::event::Timespec {
            tv_sec: 2,
            tv_nsec: 0,
        }),
    )
    .unwrap();
    assert!(fd[0].revents().contains(rustix::event::PollFlags::IN));
    let handle = sim.handle::<runtime::CanHost>("host").unwrap();
    let error = sim
        .with_component(&handle, |host, _| {
            host.send(CanFrame::new(1, 0, None, [1], 0).unwrap())
        })
        .unwrap_err();
    assert_eq!(sim.state(), SimulationState::Failed);
    assert!(Arc::ptr_eq(&error, &sim.failure().unwrap()));
    let _ = sim.close();
    clean(&owner);
}

#[test]
fn public_board_projection_validates_native_schema_and_uses_sampled_levels() {
    let mut snapshot = json!({"schema":1,"timestamp_ns":0,"board":"dm-mc02","vin_mv":24000,"gpio":{},"led":{"pin":"B0","rgb":1,"brightness":1,"source":"gpio-latch"},"key":{"pin":"A15","pressed":true,"level":0}});
    for bank in 'A'..='H' {
        snapshot["gpio"][bank.to_string()] =
            json!({"moder":0,"sampled":if bank=='B' {1} else {0},"odr":0,"output_mask":1});
    }
    assert!(profile::digital_level("gpio.B0", &snapshot).unwrap());
    assert!(!profile::digital_level("key", &snapshot).unwrap());
    snapshot["gpio"]["B"]["sampled"] = json!(65536);
    assert!(profile::validate_snapshot(&snapshot).is_err());
}

fn native_path(name: &str) -> PathBuf {
    std::env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| panic!("set {name}"))
}
fn board(sim: &HostSimulation, name: &str) -> serde_json::Value {
    sim.component(&sim.handle::<QemuMachine>(name).unwrap())
        .unwrap()
        .observe()
        .unwrap()
}
fn uart(sim: &HostSimulation, name: &str) -> Vec<u8> {
    sim.events()
        .into_iter()
        .filter_map(|event| {
            if event.source_node == name {
                if let EventPayload::Observation(HostEvent::MachineUart { data }) = event.payload {
                    return Some(data);
                }
            }
            None
        })
        .flatten()
        .collect()
}
#[test]
#[ignore = "requires retained native minimal-board and can-faults ELFs plus project QEMU"]
fn native_compiled_simulation_gpio_uart_and_can_consumer() {
    let qemu = native_path("SIM_NATIVE_QEMU");
    let elf = native_path("SIM_NATIVE_ELF");
    let mut sim = simulation(
        vec![
            machine("source", &elf, &qemu),
            machine("sink", &elf, &qemu),
            NodeSpec::new(
                "net",
                "net.digital",
                HostOptions::DigitalNet(DigitalNetOptions::default()),
            ),
        ],
        vec![
            link("source.gpio.B0.out", "net.signal"),
            link("sink.key", "net.signal"),
        ],
    );
    sim.start(None).unwrap();
    let owners = [info(&sim, "source"), info(&sim, "sink")];
    let result = (|| {
        sim.run_until(10_004_000, None)?;
        assert!(uart(&sim, "source")
            .windows(b"SIM_READY minimal-board\n".len())
            .any(|bytes| bytes == b"SIM_READY minimal-board\n"));
        assert!(uart(&sim, "sink")
            .windows(b"SIM_READY minimal-board\n".len())
            .any(|bytes| bytes == b"SIM_READY minimal-board\n"));
        assert!(!profile::digital_level("gpio.B0", &board(&sim, "source"))?);
        assert!(profile::digital_level("gpio.B0", &board(&sim, "sink"))?);
        sim.schedule_input(
            ep("source.key"),
            HostInput::Machine(MachineInput::Bool(true)),
            None,
        )?;
        sim.run_until(20_004_000, None)?;
        assert!(profile::digital_level("gpio.B0", &board(&sim, "source"))?);
        assert!(!profile::digital_level("gpio.B0", &board(&sim, "sink"))?);
        for owner in &owners {
            assert_eq!(owner.native_origin_ns, 0);
        }
        assert_eq!(board(&sim, "source")["timestamp_ns"], 20_000_000);
        Ok::<_, sim_core::Failure>(())
    })();
    let cleanup = sim.close();
    for owner in &owners {
        clean(owner);
    }
    result.unwrap();
    cleanup.unwrap();

    let elf = native_path("SIM_NATIVE_CAN_ELF");
    let mut sim = simulation(
        vec![
            machine("m", &elf, &qemu),
            NodeSpec::new(
                "bus",
                "bus.can",
                HostOptions::CanBus(CanBusOptions::default().into()),
            ),
            NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
        ],
        vec![link("m.fdcan1", "bus.can"), link("host.can", "bus.can")],
    );
    sim.start(None).unwrap();
    let owner = info(&sim, "m");
    let result = (|| {
        sim.run_until(10_004_000, None)?;
        assert!(uart(&sim, "m")
            .windows(b"SIM_READY can-faults\n".len())
            .any(|bytes| bytes == b"SIM_READY can-faults\n"));
        let nonce = 0x1234_5678u32;
        let mut payload = nonce.to_le_bytes().to_vec();
        payload.extend_from_slice(&(nonce ^ 0xa55a_96c3).to_le_bytes());
        sim.schedule_input(
            ep("host.tx"),
            HostInput::CanFrame(CanFrame::new(0x321, 0, None, payload, 0).unwrap()),
            None,
        )?;
        sim.run_until(15_004_000, None)?;
        let frames: Vec<_> = sim
            .events()
            .into_iter()
            .filter_map(|event| {
                if let EventPayload::Observation(HostEvent::MachineCanTx { frame }) = event.payload
                {
                    Some((
                        frame,
                        event.source_time_ns,
                        event.mapped_time_ns,
                        event.delivery_time_ns,
                    ))
                } else {
                    None
                }
            })
            .collect();
        let (frame, source, mapped, delivery) = frames
            .iter()
            .find(|(frame, ..)| frame.can_id() == 0x421)
            .expect("actual firmware nonce reply");
        assert_eq!(
            frame.data(),
            [0x8d, 0x2f, 0x1f, 0x7f, 0x2a, 0x3f, 0x2b, 0x1a]
        );
        assert_eq!(*source, Some(frame.timestamp_ns()));
        assert_eq!(*mapped, frame.timestamp_ns() + 4000);
        assert!(*mapped <= *delivery);
        assert!(sim.events().iter().any(|event|matches!(&event.payload,EventPayload::Observation(HostEvent::CanInputProcessed {value}) if value["completed_frames"]==1)));
        Ok::<_, sim_core::Failure>(())
    })();
    let cleanup = sim.close();
    clean(&owner);
    result.unwrap();
    cleanup.unwrap();
}

#[test]
fn canonical_can_receipt_includes_native_drops_and_output_keeps_its_source_time() {
    let (_dir, firmware, qemu) = fixture("can-good");
    let mut sim = simulation(
        vec![
            machine("m", &firmware, &qemu),
            NodeSpec::new(
                "bus",
                "bus.can",
                HostOptions::CanBus(CanBusOptions::default().into()),
            ),
            NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
        ],
        vec![link("m.fdcan1", "bus.can"), link("host.can", "bus.can")],
    );
    sim.start(None).unwrap();
    let owner = info(&sim, "m");
    sim.schedule_input(
        ep("host.tx"),
        HostInput::CanFrame(CanFrame::new(1, 0, None, [1], 0).unwrap()),
        None,
    )
    .unwrap();
    let result = sim.run_until(5000, None);
    let events = sim.events();
    let cleanup = sim.close();
    clean(&owner);
    result.unwrap();
    cleanup.unwrap();
    let receipt = events
        .iter()
        .find_map(|event| {
            if let EventPayload::Observation(HostEvent::CanInputProcessed { value }) =
                &event.payload
            {
                Some(value)
            } else {
                None
            }
        })
        .unwrap();
    assert_eq!(
        (
            receipt["completed_frames"].as_u64(),
            receipt["rx_dropped"].as_u64()
        ),
        (Some(1), Some(1))
    );
    let delivery=events.iter().find(|event|matches!(&event.payload,EventPayload::Observation(HostEvent::CanDelivery{frame,..}) if frame.can_id()==0x123)).unwrap();
    assert_eq!(
        (
            delivery.source_time_ns,
            delivery.mapped_time_ns,
            delivery.delivery_time_ns
        ),
        (Some(4990), 4990, 5000)
    );
}

#[test]
fn can_counter_overrun_and_native_tx_drop_freeze_without_publishing_a_successful_cut() {
    for mode in ["can-rx-overrun", "can-tx-drop"] {
        let (_dir, firmware, qemu) = fixture(mode);
        let mut sim = simulation(
            vec![
                machine("m", &firmware, &qemu),
                NodeSpec::new(
                    "bus",
                    "bus.can",
                    HostOptions::CanBus(CanBusOptions::default().into()),
                ),
                NodeSpec::new("host", "device.can-host", HostOptions::CanHost),
            ],
            vec![link("m.fdcan1", "bus.can"), link("host.can", "bus.can")],
        );
        sim.start(None).unwrap();
        let owner = info(&sim, "m");
        if mode == "can-rx-overrun" {
            sim.schedule_input(
                ep("host.tx"),
                HostInput::CanFrame(CanFrame::new(1, 0, None, [1], 0).unwrap()),
                None,
            )
            .unwrap();
        }
        let error = sim.run_until(5000, None).unwrap_err();
        assert_eq!(
            (sim.state(), sim.t_sim_ns()),
            (SimulationState::Failed, 4000)
        );
        assert!(error.to_string().contains(if mode == "can-rx-overrun" {
            "exceeds reserved goal"
        } else {
            "drop count changed"
        }));
        let _ = sim.close();
        clean(&owner);
    }
}
