#![cfg(feature = "lab")]
use sim_host::lab::{
    BoardCommand, ImuMode, LabInput, StartupMode,
    project::{LabProject, ProjectFile, SCHEMA},
};

#[test]
fn missing_project_remains_unsaved_and_relative_elf_uses_project_directory() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("sim.lab.json");
    let file = ProjectFile::open(&path).unwrap();
    assert!(!path.exists());
    let mut project = file.project().clone();
    assert_eq!(project.imu, ImuMode::Stationary);
    assert_eq!(
        serde_json::from_str::<LabProject>(r#"{"schema":"sim/lab-project/v1"}"#)
            .unwrap()
            .imu,
        ImuMode::Stationary,
    );
    assert!(!project.watch_elf);
    assert!(
        !serde_json::from_str::<LabProject>(r#"{"schema":"sim/lab-project/v1"}"#)
            .unwrap()
            .watch_elf
    );
    assert_eq!(
        (
            project.startup_timeout_secs,
            project.timeout_secs,
            project.user_hold_ms
        ),
        (10.0, 3600.0, 1000)
    );
    project.firmware = Some("build/firmware.elf".into());
    project.watch_elf = true;
    project.imu = ImuMode::Off;
    project.startup = StartupMode::User;
    file.save(&project).unwrap();
    let reopened = ProjectFile::open(&path).unwrap();
    let resolved = reopened.resolve(reopened.project()).unwrap();
    assert!(resolved.watch_elf);
    assert_eq!(resolved.imu, ImuMode::Off);
    assert_eq!(
        resolved.lab_options(None, None, None).unwrap().imu,
        ImuMode::Off
    );
    assert_eq!(
        resolved.firmware.unwrap(),
        directory.path().join("build/firmware.elf")
    );
    assert_eq!(
        reopened.project().firmware.as_ref().unwrap().to_str(),
        Some("build/firmware.elf")
    );
    let before = std::fs::read(&path).unwrap();
    project.timeout_secs = 1.0;
    assert!(file.save(&project).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), before);
}

#[test]
fn strict_files_reject_ambiguous_schema_shortcuts_and_oversize_input() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("sim.lab.json");
    for contents in [
        r#"{}"#,
        r#"{"schema":"other"}"#,
        r#"{"schema":"sim/lab-project/v1","extra":true}"#,
        r#"{"schema":"sim/lab-project/v1","adc":true,"adc":false}"#,
        r#"{"schema":"sim/lab-project/v1","actions":{"x":{"action":"key","pressed":true},"x":{"action":"key","pressed":false}}}"#,
        r#"{"schema":"sim/lab-project/v1","actions":{"x":{"action":"key","pressed":true,"delay":10}}}"#,
        r#"{"schema":"sim/lab-project/v1","uarts":["uart1","uart1"]}"#,
        r#"{"schema":"sim/lab-project/v1","usbip":"0.0.0.0:3240"}"#,
        r#"{"schema":"sim/lab-project/v1","startup":"wait_gdb"}"#,
        r#"{"schema":"sim/lab-project/v1","watch_elf":true}"#,
        r#"{"schema":"sim/lab-project/v1","imu":"motion"}"#,
    ] {
        std::fs::write(&path, contents).unwrap();
        assert!(ProjectFile::open(&path).is_err(), "{contents}");
    }
    std::fs::write(&path, vec![b' '; 256 * 1024 + 1]).unwrap();
    assert!(
        ProjectFile::open(path)
            .unwrap_err()
            .to_string()
            .contains("256 KiB")
    );
}

#[test]
fn one_shot_shortcuts_preserve_bytes_and_use_shared_channel_validation() {
    let mut project: LabProject = serde_json::from_value(serde_json::json!({
        "schema":SCHEMA,
        "actions":{
            "uart5-send":{"action":"uart","endpoint":"uart5","format":"hex","ending":"crlf","data":"00 ff c3 a9"},
            "can-send":{"action":"can","endpoint":"can1","id":291,"data":"aa bb"},
            "vin":{"action":"vin","millivolts":24000}
        }
    })).unwrap();
    let actions = project.commands().unwrap();
    let BoardCommand::Input(LabInput::Uart { endpoint, data }) = &actions["uart5-send"] else {
        panic!()
    };
    assert_eq!(
        (endpoint.as_str(), data.as_slice()),
        ("uart5", &[0, 255, 195, 169, 13, 10][..])
    );
    let BoardCommand::Input(LabInput::Can { frame, .. }) = &actions["can-send"] else {
        panic!()
    };
    assert_eq!((frame.can_id(), frame.data()), (0x123, &[0xaa, 0xbb][..]));
    project.uarts.retain(|port| port != "uart5");
    assert!(
        project
            .commands()
            .unwrap_err()
            .to_string()
            .contains("disabled channel")
    );
    project.uarts.push("uart5".into());
    project.actions.insert(
        "bad-can".into(),
        serde_json::from_value(serde_json::json!({
            "action":"can","endpoint":"can1","id":0x800,"data":""
        }))
        .unwrap(),
    );
    assert!(project.validate().is_err());
}

#[test]
fn debugger_exports_attach_to_native_server_and_quote_local_elf() {
    let project = LabProject {
        firmware: Some("local board/firmware.elf".into()),
        gdb_port: Some(4321),
        ..Default::default()
    };
    let config = project.ide_config().unwrap();
    let vscode = &config["vscode"]["configurations"][0];
    assert_eq!(
        (
            vscode["request"].as_str(),
            vscode["servertype"].as_str(),
            vscode["gdbTarget"].as_str()
        ),
        (Some("attach"), Some("external"), Some("127.0.0.1:4321"))
    );
    assert_eq!(
        config["gdb"],
        "file \"local board/firmware.elf\"\ntarget remote 127.0.0.1:4321\n"
    );
    assert_eq!(config["clion"]["configuration"], "Remote Debug");
}
