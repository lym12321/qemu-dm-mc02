//! A finite experiment using the existing minimal-board firmware and app owner.
#[cfg(target_os = "linux")]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    use clap::Parser;
    use sim_core::{Endpoint, NodeSpec, RunMode, SimulationOptions, Topology};
    use sim_host::{
        apps::experiment::run_experiment,
        assertions::{Assertion, Condition},
        config::scene::{Experiment, ExperimentAssertion, ExperimentInput, Origin, Scene},
        event_json::result_json,
        machine::QemuOptions,
        profile::MachineInput,
        runtime::{HostInput, HostOptions},
    };
    use std::path::PathBuf;

    #[derive(Parser)]
    struct Options {
        #[arg(long)]
        qemu: PathBuf,
        #[arg(long)]
        firmware: PathBuf,
        #[arg(long)]
        backend_root: Option<PathBuf>,
    }
    let args = Options::parse();
    let mut machine = QemuOptions::new(args.firmware.canonicalize()?);
    machine.qemu = Some(args.qemu.canonicalize()?);
    machine.backend_root = args.backend_root.map(std::fs::canonicalize).transpose()?;
    let inputs = [(40_000_000, true), (90_000_000, false)]
        .into_iter()
        .map(|(at_ns, level)| {
            Ok(ExperimentInput {
                at_ns,
                endpoint: Endpoint::parse("main.key")?,
                payload: HostInput::Machine(MachineInput::Bool(level)),
                origin: Origin::Session,
            })
        })
        .collect::<sim_core::Result<_>>()?;
    let assertions = [
        (
            200_000_000,
            "main.uart1",
            Condition::UartContains(b"SIM_READY minimal-board\n".to_vec()),
        ),
        (
            200_000_000,
            "main.uart1",
            Condition::UartContains(b"KEY pressed B0=1\nKEY released B0=0\n".to_vec()),
        ),
        (50_000_000, "main.gpio.B0", Condition::DigitalEquals(true)),
    ]
    .into_iter()
    .map(|(before_ns, endpoint, condition)| ExperimentAssertion {
        assertion: Assertion {
            before_ns,
            endpoint: endpoint.into(),
            condition,
        },
        origin: Origin::Session,
    })
    .collect();
    let scene = Scene {
        source: PathBuf::from("examples/experiment"),
        topology: Topology::new(
            vec![NodeSpec::new(
                "main",
                "machine.qemu",
                HostOptions::Qemu(machine),
            )],
            vec![],
        )?,
        mode: RunMode::Stepped,
        quantum_ns: 1_000_000,
        duration_ns: 200_000_000,
        experiment: Some(Experiment {
            inputs,
            assertions,
            ..Default::default()
        }),
    };
    let result =
        run_experiment(&scene, SimulationOptions::default(), None).inspect_err(|error| {
            for note in &error.notes {
                eprintln!("cleanup: {note}");
            }
        })?;
    println!("{}", serde_json::to_string_pretty(&result_json(&result))?);
    if !result.passed() {
        return Err("experiment assertions failed".into());
    }
    Ok(())
}
#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("The native machine example requires Linux.");
    std::process::exit(78);
}
