#![cfg(all(target_os = "linux", feature = "machine"))]
use sim_core::{
    BuildContext, Capability, Component, Direction, Endpoint, EventPayload, Factory, LinkSpec,
    NodeDescription, NodeSpec, PortCategory, PortSpec, Result, Session, SimulationOptions,
    StepContext, StepResult, Stepper, Topology,
};
use sim_host::{
    fabric::imu::ImuAttachment,
    machine::{QemuMachine, QemuOptions},
    profile::MachineInput,
    runtime::{self, HostContext, HostEvent, HostInput, HostOptions, HostSimulation},
    signals::{ImuSample, ImuSignalSample},
    step_wire,
};
use std::{
    cell::Cell,
    fs,
    path::{Path, PathBuf},
    rc::Rc,
    time::Duration,
};
const ORIGIN: u64 = 4000;
fn signal(mapped: u64) -> ImuSignalSample {
    ImuSignalSample {
        sample: ImuSample {
            gyro_rad_s: [100., -200., 50.].map(|v| v * std::f64::consts::PI / 180.),
            accel_m_s2: [9.80665, -4.903325, 2.4516625],
        },
        source_time_ns: Some(mapped - ORIGIN),
        mapped_time_ns: mapped,
    }
}
struct Source {
    value: Rc<Cell<ImuSignalSample>>,
}
impl Stepper<HostInput, HostEvent> for Source {
    fn step(&mut self, step: &StepContext, _: &HostContext) -> Result<StepResult<HostEvent>> {
        self.value.set(signal(step.t_sim_ns));
        Ok(StepResult::reached(step.t_sim_ns))
    }
}
impl Component<HostInput, HostEvent> for Source {
    fn reset(&mut self, session: Session, _: &HostContext) -> Result<()> {
        self.value.set(signal(session.t_sim_ns));
        Ok(())
    }
    fn close(&mut self) -> Result<()> {
        Ok(())
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        Some(self)
    }
}
struct SourceFactory;
impl Factory<HostOptions, HostInput, HostEvent> for SourceFactory {
    fn describe(&self, _: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let mut port = PortSpec::new("imu", "imu");
        port.direction = Direction::Out;
        port.category = PortCategory::Signal;
        Ok(NodeDescription::new(vec![port], [Capability::Step]))
    }
    fn create(
        &self,
        _: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let value = Rc::new(Cell::new(signal(ORIGIN)));
        let reader = value.clone();
        build.attach::<_, ()>(
            "imu",
            ImuAttachment::Source(Rc::new(move || Ok(reader.get()))),
        )?;
        Ok(Box::new(Source { value }))
    }
}
fn ep(value: &str) -> Endpoint {
    Endpoint::parse(value).unwrap()
}
fn build(options: QemuOptions, quantum: u64) -> HostSimulation {
    let nodes = vec![
        NodeSpec::new("board", "machine.qemu", HostOptions::Qemu(options)),
        NodeSpec::new("world", "fixture.si", HostOptions::CanHost),
        NodeSpec::new("wire", "net.imu", HostOptions::ImuNet(Default::default())),
    ];
    let links = vec![
        LinkSpec::new(ep("world.imu"), ep("wire.signal")),
        LinkSpec::new(ep("wire.signal"), ep("board.imu")),
    ];
    let mut registry = runtime::registry().unwrap();
    registry.register("fixture.si", SourceFactory).unwrap();
    HostSimulation::new(
        Topology::new(nodes, links).unwrap(),
        registry,
        SimulationOptions {
            initial_session: Session::new(1, ORIGIN).unwrap(),
            host_timeout: Some(Duration::from_secs(10)),
            quantum_ns: quantum,
            ..Default::default()
        },
    )
    .unwrap()
}
#[test]
fn origin_is_observation_only_then_adc_and_si_share_one_step_and_done_is_distinct() {
    for mode in ["imu-good", "imu-missing"] {
        let directory = tempfile::tempdir().unwrap();
        let firmware = directory.path().join("fixture.elf");
        fs::write(&firmware, mode).unwrap();
        let steps = firmware.with_extension("steps");
        let mut options = QemuOptions::new(&firmware);
        options.qemu =
            Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/support/machine_qemu.py"));
        options.adc = true;
        options.uarts.clear();
        let mut sim = build(options, 1000);
        sim.start(None).unwrap();
        assert!(!steps.exists());
        assert_eq!(
            sim.events()
                .iter()
                .filter(|e| e.kind == "imu-input-initial")
                .count(),
            1
        );
        sim.schedule_input(
            ep("board.adc1.ch4"),
            HostInput::Machine(MachineInput::AdcRaw(12345)),
            Some(5000),
        )
        .unwrap();
        let result = sim.run_until(6000, None);
        let cleanup = sim.close();
        result.unwrap();
        cleanup.unwrap();
        let rows: Vec<serde_json::Value> = fs::read_to_string(steps)
            .unwrap()
            .lines()
            .map(|v| serde_json::from_str(v).unwrap())
            .collect();
        assert_eq!(rows.len(), 2);
        let encoded = rows[0]["payload"]
            .as_str()
            .unwrap()
            .as_bytes()
            .chunks_exact(2)
            .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
            .collect::<Vec<_>>();
        let step_wire::StepInput::Sections(sections) =
            step_wire::StepInput::decode(&encoded).unwrap()
        else {
            panic!("expected sections")
        };
        assert_eq!(sections.len(), 2);
        let step_wire::InputSection::Imu(imu) = &sections[0] else {
            panic!("expected IMU first")
        };
        assert_eq!(
            (imu.gyro_dps, imu.accel_g, imu.sensor_time_ns, imu.sample_id),
            ([100., -200., 50.], [1., -0.5, 0.25], 5000, 0)
        );
        let events = sim.events();
        let acceptance = events
            .iter()
            .find(|e| e.kind == "imu-input-accepted")
            .unwrap();
        let done = events.iter().find(|e| e.kind == "imu-consumption").unwrap();
        let (
            EventPayload::Observation(HostEvent::Cosim { value: ack }),
            EventPayload::Observation(HostEvent::Cosim { value: done_value }),
        ) = (&acceptance.payload, &done.payload)
        else {
            panic!("expected public cosim values")
        };
        assert_eq!(ack["input"]["source_time_ns"], 1000);
        assert_eq!(
            (
                done_value["consumed_mask"].as_u64().unwrap(),
                done_value["missing_mask"].as_u64().unwrap()
            ),
            if mode == "imu-good" { (3, 0) } else { (1, 2) }
        );
        assert!(acceptance.sequence < done.sequence);
        assert_eq!((done.source_time_ns, done.mapped_time_ns), (None, 5000));
    }
}
#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ADC_IMU_ELF"]
fn native_si_wire_units_are_read_by_both_bmi088_dies_before_guest_report() {
    let mut options = QemuOptions::new(PathBuf::from(
        std::env::var_os("SIM_NATIVE_ADC_IMU_ELF").unwrap(),
    ));
    options.qemu = Some(PathBuf::from(std::env::var_os("SIM_NATIVE_QEMU").unwrap()));
    options.adc = true;
    let mut sim = build(options, 1_000_000);
    sim.start(None).unwrap();
    let handle = sim.handle::<QemuMachine>("board").unwrap();
    let info = sim.component(&handle).unwrap().connection_info().unwrap();
    let result = (|| {
        for (cut, input) in [
            (35_004_000, MachineInput::AdcRaw(12345)),
            (55_004_000, MachineInput::AdcVoltage(1_650_000)),
        ] {
            sim.schedule_input(ep("board.adc1.ch4"), HostInput::Machine(input), Some(cut))?;
        }
        sim.run_until(80_004_000, None)?;
        let output = uart(&sim);
        let text = String::from_utf8(output).unwrap();
        assert!(text.contains("ADC_IMU_PASS\n"), "{text}");
        let rows = text
            .lines()
            .filter(|line| line.starts_with("ADC_IMU_READ "))
            .collect::<Vec<_>>();
        assert_eq!(rows.len(), 2);
        let mut last_time = 0;
        for (i, line) in rows.iter().enumerate() {
            assert!(
                line.contains(if i == 0 { "adc=3039" } else { "adc=8000" }),
                "{line}"
            );
            for (key, values, range) in [
                ("g=", [100., -200., 50.], 1000.),
                ("a=", [1., -0.5, 0.25], 6.),
            ] {
                let hex = line
                    .split_whitespace()
                    .find_map(|v| v.strip_prefix(key))
                    .unwrap();
                for (axis, value) in values.into_iter().enumerate() {
                    let start = axis * 4;
                    let lo = u8::from_str_radix(&hex[start..start + 2], 16).unwrap();
                    let hi = u8::from_str_radix(&hex[start + 2..start + 4], 16).unwrap();
                    let actual = i16::from_le_bytes([lo, hi]) as f64;
                    assert!(
                        (actual - value * 32768. / range).abs() <= 0.5,
                        "{line} {key} axis {axis} {actual}"
                    );
                }
            }
            let ticks = u32::from_str_radix(
                line.split_whitespace()
                    .find_map(|v| v.strip_prefix("t="))
                    .unwrap(),
                16,
            )
            .unwrap();
            assert!(ticks > last_time);
            last_time = ticks;
        }
        assert!(sim.events().iter().any(|event|event.kind=="imu-consumption" && matches!(&event.payload,EventPayload::Observation(HostEvent::Cosim {value}) if value["status"]==0&&value["consumed_mask"]==3&&value["missing_mask"]==0)));
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
        .filter_map(|e| match &e.payload {
            EventPayload::Observation(HostEvent::MachineUart { data }) => Some(data.clone()),
            _ => None,
        })
        .flatten()
        .collect()
}
