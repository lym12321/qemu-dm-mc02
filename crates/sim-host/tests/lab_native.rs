#![cfg(all(target_os = "linux", feature = "lab"))]
use sim_host::{
    boards::digital_level,
    lab::{
        AdcPausedError, BoardCommand, ConnectedLab, ImuMode, LabInput, LabOptions, StartupMode,
        channels::{AdcValue, ChannelData},
        with_lab,
    },
    qmp::commands,
};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

fn options(asset: &str) -> LabOptions {
    let mut options = LabOptions::new(std::env::var_os(asset).expect(asset));
    options.qemu = Some(PathBuf::from(std::env::var_os("SIM_NATIVE_QEMU").unwrap()));
    options.startup_timeout = Duration::from_secs(3);
    options.timeout = Duration::from_secs(5);
    options
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ELF"]
fn user_start_uses_virtual_hold_and_manual_key_cancels_automatic_release() {
    let mut options = options("SIM_NATIVE_ELF");
    options.imu = ImuMode::Stationary;
    let (pid, directory) = with_lab(options, |lab| {
        assert!(
            lab.inject(LabInput::Adc {
                channel: 4,
                value: AdcValue::Raw(0),
            })
            .unwrap_err()
            .to_string()
            .contains("ADC co-sim is disabled")
        );
        assert!(lab.start(StartupMode::WaitGdb, Duration::ZERO).is_err());
        assert!(lab.start(StartupMode::User, Duration::ZERO).is_err());
        lab.start(StartupMode::User, Duration::from_secs(1))?;
        lab.apply(BoardCommand::Pause)?;
        let paused = lab.snapshot()?;
        let paused_ns = paused["timestamp_ns"].as_u64().unwrap();
        assert!(
            paused_ns < 1_000_000_000,
            "startup consumed the entire hold"
        );
        lab.wait(None, Duration::from_millis(10))?;
        let (still_paused, _) = lab.pump()?;
        assert_eq!(still_paused["timestamp_ns"], paused["timestamp_ns"]);
        assert_eq!(still_paused["key"]["pressed"], true);
        lab.apply(BoardCommand::Resume)?;
        loop {
            let (snapshot, _) = lab.pump()?;
            if snapshot["key"]["pressed"] == false {
                assert!(snapshot["timestamp_ns"].as_u64().unwrap() >= 1_000_000_000);
                break;
            }
            lab.wait(None, Duration::from_millis(5))?;
        }
        lab.pause()?;
        let origin = lab.snapshot()?["timestamp_ns"].as_u64().unwrap();
        lab.start(StartupMode::User, Duration::from_millis(20))?;
        assert!(
            lab.apply(BoardCommand::Input(LabInput::Key(true)))?
                .is_some()
        );
        loop {
            let (snapshot, _) = lab.pump()?;
            if snapshot["timestamp_ns"].as_u64().unwrap() >= origin + 40_000_000 {
                assert_eq!(snapshot["key"]["pressed"], true);
                break;
            }
            lab.wait(None, Duration::from_millis(5))?;
        }
        lab.apply(BoardCommand::Input(LabInput::Key(false)))?;
        Ok((lab.pid(), lab.directory()))
    })
    .unwrap();
    cleaned(pid, directory);
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
#[ignore = "requires SIM_NATIVE_QEMU, SIM_NATIVE_ELF and SIM_NATIVE_ADC_ELF"]
fn ordinary_lab_keeps_reset_image_when_the_linker_overwrites_its_source() {
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("firmware.elf");
    fs::copy(std::env::var_os("SIM_NATIVE_ELF").unwrap(), &source).unwrap();
    let mut options = options("SIM_NATIVE_ELF");
    options.firmware = source.clone();
    let mut image = None;
    let (pid, private) = with_lab(options, |lab| {
        let args = fs::read(format!("/proc/{}/cmdline", lab.pid())).unwrap();
        let args: Vec<_> = args.split(|byte| *byte == 0).collect();
        let path = args.windows(2).find(|pair| pair[0] == b"-kernel").unwrap()[1];
        let loaded = PathBuf::from(String::from_utf8(path.to_vec()).unwrap());
        assert_ne!(loaded, source);
        assert!(
            !fs::read_to_string(format!("/proc/{}/maps", lab.pid()))
                .unwrap()
                .contains(source.to_str().unwrap())
        );
        image = Some(loaded);
        lab.resume()?;
        wait_uart(lab, &mut Vec::new(), b"SIM_READY minimal-board\n")?;
        // The zero-length output used to SIGBUS QEMU on the next native reset;
        // a different complete ELF also must not silently change reset data.
        for contents in [
            vec![],
            fs::read(std::env::var_os("SIM_NATIVE_ADC_ELF").unwrap()).unwrap(),
        ] {
            fs::write(&source, contents).unwrap();
            lab.command(commands::system_reset {})?;
            wait_uart(lab, &mut Vec::new(), b"SIM_READY minimal-board\n")?;
        }
        Ok((lab.pid(), lab.directory()))
    })
    .unwrap();
    cleaned(pid, private);
    assert!(
        !image.unwrap().exists(),
        "loaded image outlived native cleanup"
    );
}

#[test]
fn startup_cancellation_closes_a_blocked_qmp_greeting_and_reaps_the_original_child() {
    let directory = tempfile::tempdir().unwrap();
    let record = directory.path().join("connected.json");
    let executable = directory.path().join("qemu-fixture");
    fs::write(&executable, format!(
        "#!{}\nimport json,os,signal,socket,sys\npath=sys.argv[sys.argv.index('-qmp')+1].split(',')[0][5:]\ns=socket.socket(socket.AF_UNIX)\ns.bind(path)\ns.listen()\npeer,_=s.accept()\nwith open({},'w') as f: json.dump([os.getpid(),os.path.dirname(path)],f)\nsignal.pause()\n",
        which::which("python3").unwrap().display(), serde_json::json!(record),
    )).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    let firmware = directory.path().join("fixture.elf");
    // ELF gABI: ARM ELF32 executable with one four-byte PT_LOAD. The QMP
    // fixture never executes it, but acquisition still admits real ELF input.
    let mut elf = vec![0; 88];
    elf[..7].copy_from_slice(b"\x7fELF\x01\x01\x01");
    for (offset, value) in [(16, 2u16), (18, 40), (40, 52), (42, 32), (44, 1)] {
        elf[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
    }
    for (offset, value) in [(20, 1u32), (28, 52), (52, 1), (56, 84), (68, 4), (72, 4)] {
        elf[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
    }
    fs::write(&firmware, elf).unwrap();
    let cancellation = Arc::new(AtomicBool::new(false));
    let mut options = LabOptions::new(firmware);
    options.qemu = Some(executable);
    options.uarts.clear();
    options.startup_timeout = Duration::from_secs(10);
    options.timeout = Duration::from_secs(15);
    options.cancellation = Some(cancellation.clone());
    let before = Instant::now();
    let error = thread::scope(|scope| {
        scope.spawn(|| {
            let deadline = Instant::now() + Duration::from_secs(2);
            while fs::read(&record)
                .ok()
                .is_none_or(|bytes| serde_json::from_slice::<(u32, PathBuf)>(&bytes).is_err())
            {
                assert!(Instant::now() < deadline, "fixture did not accept QMP");
                thread::sleep(Duration::from_millis(2));
            }
            cancellation.store(true, Ordering::Relaxed);
        });
        with_lab(options, |_| -> sim_core::Result<()> {
            panic!("blocked negotiation reached consumer")
        })
        .unwrap_err()
    });
    assert!(matches!(
        error.primary.downcast_ref::<sim_core::Error>(),
        Some(sim_core::Error::Interrupted(_))
    ));
    assert!(!error.cleanup_failed, "{:?}", error.notes);
    assert!(before.elapsed() < Duration::from_secs(2));
    let (pid, private): (u32, PathBuf) =
        serde_json::from_slice(&fs::read(record).unwrap()).unwrap();
    cleaned(pid, private);
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
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ADC_IMU_ELF"]
fn stationary_imu_and_adc_survive_native_reset_until_real_spi_readback() {
    let mut options = options("SIM_NATIVE_ADC_IMU_ELF");
    options.adc = true;
    options.imu = ImuMode::Stationary;
    let (pid, directory) = with_lab(options, |lab| {
        // Pumping a paused board must not manufacture input time or samples.
        for _ in 0..2 {
            assert_eq!(lab.pump()?.0["timestamp_ns"], 0);
        }
        for pass in 0..2 {
            lab.resume()?;
            let mut text = Vec::new();
            wait_uart(lab, &mut text, b"ADC_IMU_READY\n")?;
            lab.inject(LabInput::Adc {
                channel: 4,
                value: AdcValue::Raw(12345),
            })?;
            let mut first_report = Vec::new();
            wait_uart(lab, &mut first_report, b"\n")?;
            text.extend(first_report);
            let first = lab.snapshot()?["timestamp_ns"].as_u64().unwrap();
            // This fixture configures 100Hz ODR. Wait using actual native time so
            // a new sample can be read after the guest cleared its first DRDY.
            while lab.pump()?.0["timestamp_ns"].as_u64().unwrap() < first + 20_000_000 {
                lab.wait(None, Duration::from_millis(2))?;
            }
            lab.inject(LabInput::Adc {
                channel: 4,
                value: AdcValue::Voltage(1_650_000),
            })?;
            wait_uart(lab, &mut text, b"ADC_IMU_PASS\n")?;
            let output = String::from_utf8(text).unwrap();
            let mut previous_ticks = 0;
            for (row, expected_adc) in output
                .lines()
                .filter(|line| line.starts_with("ADC_IMU_READ "))
                .zip(["3039", "8000"])
            {
                // Bosch Rev1.9 §5.3.4: round(32768 / 6)=5461=0x1555 for +1g.
                assert!(
                    row.contains(&format!(
                        "adc={expected_adc} g=000000000000 a=000000005515 "
                    )),
                    "{row}"
                );
                let ticks = u32::from_str_radix(row.split(" t=").nth(1).unwrap(), 16).unwrap();
                assert!(ticks > previous_ticks, "{row}");
                previous_ticks = ticks;
            }
            assert_eq!(output.matches("ADC_IMU_READ ").count(), 2, "{output}");
            lab.pause()?;
            let paused = lab.pump()?.0;
            assert_eq!(lab.pump()?.0["timestamp_ns"], paused["timestamp_ns"]);
            if pass == 0 {
                lab.command(commands::system_reset {})?;
                loop {
                    let (snapshot, events) = lab.pump()?;
                    assert_eq!(snapshot["timestamp_ns"], paused["timestamp_ns"]);
                    if let Some(event) = events
                        .iter()
                        .find(|event| matches!(event.data, ChannelData::Reset))
                    {
                        assert_eq!(event.endpoint, "cosim");
                        assert_eq!(event.timestamp_ns, paused["timestamp_ns"].as_u64().unwrap());
                        assert!(!lab.command(commands::query_status {})?.running);
                        break;
                    }
                    lab.wait(None, Duration::from_millis(2))?;
                }
            }
        }
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
