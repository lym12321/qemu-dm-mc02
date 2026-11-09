#![cfg(all(target_os = "linux", feature = "machine"))]
use sim_core::{Endpoint, EventPayload, NodeSpec, Session, SimulationOptions, Topology};
use sim_host::{
    machine::{QemuMachine, QemuOptions},
    profile::{self, MachineInput},
    runtime::{self, HostEvent, HostInput, HostOptions, HostSimulation},
    step_wire,
};
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};
fn ep(port: &str) -> Endpoint {
    Endpoint::parse(&format!("board.{port}")).unwrap()
}
fn node(options: QemuOptions) -> NodeSpec<HostOptions> {
    NodeSpec::new("board", "machine.qemu", HostOptions::Qemu(options))
}
fn build(options: QemuOptions) -> HostSimulation {
    HostSimulation::new(
        Topology::new(vec![node(options)], vec![]).unwrap(),
        runtime::registry().unwrap(),
        SimulationOptions {
            initial_session: Session::new(1, 4000).unwrap(),
            host_timeout: Some(Duration::from_secs(10)),
            quantum_ns: 1_000_000,
            ..Default::default()
        },
    )
    .unwrap()
}
fn fixture() -> (tempfile::TempDir, QemuOptions) {
    let directory = tempfile::tempdir().unwrap();
    let firmware = directory.path().join("fixture.elf");
    fs::write(&firmware, "adc-good").unwrap();
    let mut options = QemuOptions::new(firmware);
    options.qemu =
        Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/support/machine_qemu.py"));
    options.adc = true;
    options.uarts.clear();
    (directory, options)
}
#[test]
fn adc_profile_admits_canonical_channels_and_voltage_before_io() {
    let (_, options) = fixture();
    let spec = node(options);
    for (port, input) in [
        ("adc1.ch0", MachineInput::AdcRaw(0)),
        ("adc1.ch31", MachineInput::AdcRaw(u16::MAX)),
        ("adc1.ch4", MachineInput::AdcVoltage(3_300_000)),
    ] {
        assert!(profile::validate_input(&spec, port, HostInput::Machine(input), &[]).is_ok());
    }
    for port in ["adc1.ch32", "adc1.ch04", "adc2.ch4", "adc1.ch-1"] {
        assert!(profile::validate_input(
            &spec,
            port,
            HostInput::Machine(MachineInput::AdcRaw(1)),
            &[]
        )
        .is_err());
    }
    assert!(profile::validate_input(
        &spec,
        "adc1.ch4",
        HostInput::Machine(MachineInput::AdcVoltage(u32::MAX)),
        &[]
    )
    .is_err());
    assert!(profile::validate_input(
        &node(QemuOptions::new("x")),
        "adc1.ch4",
        HostInput::Machine(MachineInput::AdcRaw(1)),
        &[]
    )
    .is_err());
}
#[test]
fn one_stabilized_cut_combines_adc_channels_and_emits_acceptance_without_consumption() {
    let (_directory, options) = fixture();
    let steps = options.firmware.with_extension("steps");
    let mut sim = build(options);
    sim.start(None).unwrap();
    sim.run_until(5000, None).unwrap();
    sim.schedule_input(
        ep("adc1.ch4"),
        HostInput::Machine(MachineInput::AdcRaw(12345)),
        None,
    )
    .unwrap();
    sim.schedule_input(
        ep("adc1.ch7"),
        HostInput::Machine(MachineInput::AdcVoltage(1_650_000)),
        None,
    )
    .unwrap();
    let result = sim.run_until(6000, None);
    let cleanup = sim.close();
    result.unwrap();
    cleanup.unwrap();
    let records: Vec<serde_json::Value> = fs::read_to_string(steps)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records.len(), 1);
    let body = (0..records[0]["payload"].as_str().unwrap().len())
        .step_by(2)
        .map(|index| {
            u8::from_str_radix(
                &records[0]["payload"].as_str().unwrap()[index..index + 2],
                16,
            )
            .unwrap()
        })
        .collect::<Vec<_>>();
    assert_eq!(
        step_wire::StepInput::decode(&body).unwrap(),
        step_wire::StepInput::Sections(vec![
            step_wire::InputSection::Adc(step_wire::AdcInput {
                channel: 4,
                raw: 12345
            }),
            step_wire::InputSection::Voltage(step_wire::AdcVoltage {
                channel: 7,
                flags: 0,
                voltage_uv: 1_650_000
            })
        ])
    );
    let accepted = sim
        .events()
        .into_iter()
        .filter(|event| event.kind == "adc-input-accepted")
        .collect::<Vec<_>>();
    assert_eq!(accepted.len(), 2);
    for event in accepted {
        assert_eq!(
            (
                event.source_time_ns,
                event.mapped_time_ns,
                event.delivery_time_ns
            ),
            (None, 5000, 5000)
        );
    }
    assert!(!sim
        .events()
        .iter()
        .any(|event| event.kind == "imu-consumption"));
}
#[test]
fn duplicate_origin_or_oversize_adc_batch_is_terminal_before_sending_a_step() {
    for mode in ["origin", "duplicate", "capacity"] {
        let (_directory, options) = fixture();
        let steps = options.firmware.with_extension("steps");
        let mut sim = build(options);
        sim.start(None).unwrap();
        if mode != "origin" {
            sim.run_until(5000, None).unwrap();
        }
        sim.schedule_input(
            ep("adc1.ch4"),
            HostInput::Machine(MachineInput::AdcRaw(1)),
            None,
        )
        .unwrap();
        if mode == "duplicate" {
            sim.schedule_input(
                ep("adc1.ch4"),
                HostInput::Machine(MachineInput::AdcRaw(2)),
                None,
            )
            .unwrap();
        }
        if mode == "capacity" {
            for channel in (0..17).filter(|channel| *channel != 4) {
                sim.schedule_input(
                    ep(&format!("adc1.ch{channel}")),
                    HostInput::Machine(MachineInput::AdcRaw(1)),
                    None,
                )
                .unwrap();
            }
        }
        assert!(sim.run_until(6000, None).is_err());
        let _ = sim.close();
        assert!(!steps.exists());
    }
}
#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ADC_ELF"]
fn native_adc_ack_then_actual_guest_adc_dr_readback() {
    let mut options = QemuOptions::new(PathBuf::from(
        std::env::var_os("SIM_NATIVE_ADC_ELF").unwrap(),
    ));
    options.qemu = Some(PathBuf::from(std::env::var_os("SIM_NATIVE_QEMU").unwrap()));
    options.adc = true;
    let mut sim = build(options);
    sim.start(None).unwrap();
    let handle = sim.handle::<QemuMachine>("board").unwrap();
    let info = sim.component(&handle).unwrap().connection_info().unwrap();
    let result = (|| {
        sim.run_until(5_004_000, None)?;
        assert!(uart(&sim).windows(10).any(|v| v == b"LAB_READY\n"));
        sim.schedule_input(
            ep("adc1.ch4"),
            HostInput::Machine(MachineInput::AdcRaw(12345)),
            None,
        )?;
        sim.run_until(10_004_000, None)?;
        assert!(
            uart(&sim).windows(10).any(|v| v == b"ADC=12345\n"),
            "{:?}",
            uart(&sim)
        );
        sim.schedule_input(
            ep("adc1.ch4"),
            HostInput::Machine(MachineInput::AdcVoltage(1_650_000)),
            None,
        )?;
        sim.run_until(15_004_000, None)?;
        assert!(
            uart(&sim).windows(10).any(|v| v == b"ADC=32768\n"),
            "{:?}",
            uart(&sim)
        );
        let accepted = sim
            .events()
            .iter()
            .filter(|e| e.kind == "adc-input-accepted")
            .count();
        assert_eq!(accepted, 2);
        Ok::<_, sim_core::Failure>(())
    })();
    let cleanup = sim.close();
    assert!(!Path::new(&format!("/proc/{}", info.pid)).exists());
    assert!(!info.directory.unwrap().exists());
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
