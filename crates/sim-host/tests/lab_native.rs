#![cfg(all(target_os = "linux", feature = "lab"))]
use sim_host::{
    boards::digital_level,
    lab::{
        channels::{AdcValue, ChannelData},
        with_lab, AdcPausedError, ConnectedLab, LabInput, LabOptions,
    },
    qmp::commands,
};
use std::{path::PathBuf, sync::Arc, time::Duration};

fn options(asset: &str) -> LabOptions {
    let mut options = LabOptions::new(std::env::var_os(asset).expect(asset));
    options.qemu = Some(PathBuf::from(std::env::var_os("SIM_NATIVE_QEMU").unwrap()));
    options.startup_timeout = Duration::from_secs(3);
    options.timeout = Duration::from_secs(5);
    options
}
fn wait_uart(
    lab: &mut ConnectedLab<'_>,
    text: &mut Vec<u8>,
    expected: &[u8],
) -> sim_core::Result<()> {
    while !text.windows(expected.len()).any(|bytes| bytes == expected) {
        let (snapshot, events) = lab.pump()?;
        for event in events {
            assert!(event.timestamp_ns <= snapshot["timestamp_ns"].as_u64().unwrap());
            if let ChannelData::Uart(data) = event.data {
                text.extend(data);
            }
        }
        lab.wait(None, Duration::from_millis(5))?;
    }
    Ok(())
}
fn cleaned(pid: u32, directory: PathBuf) {
    assert!(!PathBuf::from(format!("/proc/{pid}")).exists());
    assert!(!directory.exists());
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ELF"]
fn free_run_lab_reads_native_snapshot_uart_and_key_then_cleans_before_return() {
    let (pid, directory) = with_lab(options("SIM_NATIVE_ELF"), |lab| {
        assert!(!lab.command(commands::query_status {})?.running);
        let initial = lab.snapshot()?;
        assert_eq!(initial["timestamp_ns"], 0);
        lab.set_vin(12345)?;
        assert_eq!(lab.snapshot()?["vin_mv"], 12345);
        lab.resume()?;
        let mut text = Vec::new();
        wait_uart(lab, &mut text, b"SIM_READY minimal-board\n")?;
        lab.inject(LabInput::Key(true))?;
        wait_uart(lab, &mut text, b"KEY pressed B0=1\n")?;
        lab.pause()?;
        let (snapshot, _) = lab.pump()?;
        assert!(snapshot["timestamp_ns"].as_u64().unwrap() > 0);
        assert!(digital_level("gpio.B0", &snapshot)?);
        assert!(!lab.command(commands::query_status {})?.running);
        Ok((lab.pid(), lab.directory()))
    })
    .unwrap();
    cleaned(pid, directory);
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ADC_ELF"]
fn free_run_v1_adc_refuses_native_pause_then_guest_reads_raw_and_voltage() {
    let mut options = options("SIM_NATIVE_ADC_ELF");
    options.adc = true;
    let (pid, directory) = with_lab(options, |lab| {
        let error = lab
            .inject(LabInput::Adc {
                channel: 4,
                value: AdcValue::Raw(12345),
            })
            .unwrap_err();
        assert!(error.downcast_ref::<AdcPausedError>().is_some());
        lab.resume()?;
        let mut text = Vec::new();
        wait_uart(lab, &mut text, b"LAB_READY\n")?;
        let first = lab.inject(LabInput::Adc {
            channel: 4,
            value: AdcValue::Raw(12345),
        })?;
        wait_uart(lab, &mut text, b"ADC=12345\n")?;
        let second = lab.inject(LabInput::Adc {
            channel: 4,
            value: AdcValue::Voltage(1_650_000),
        })?;
        assert!(second > first);
        wait_uart(lab, &mut text, b"ADC=32768\n")?;
        lab.pause()?;
        Ok((lab.pid(), lab.directory()))
    })
    .unwrap();
    cleaned(pid, directory);
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ELF"]
fn free_run_consumer_failure_keeps_identity_and_reaps_native_resources() {
    let primary =
        Arc::new(std::io::Error::other("independent consumer failure")) as sim_core::Failure;
    let mut identity = None;
    let error = with_lab(options("SIM_NATIVE_ELF"), |lab| {
        identity = Some((lab.pid(), lab.directory()));
        lab.resume()?;
        Err::<(), _>(primary.clone())
    })
    .unwrap_err();
    assert!(Arc::ptr_eq(&primary, &error.primary));
    let (pid, directory) = identity.unwrap();
    cleaned(pid, directory);
}
