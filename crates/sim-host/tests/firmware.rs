//! Native process fixtures verify launcher behavior, not ARM firmware execution.
#![cfg(unix)]

use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::Path;
use std::process::{Command, Output};
use std::time::{Duration, Instant};
use tempfile::{tempdir, TempDir};

struct Fixture {
    directory: TempDir,
}

impl Fixture {
    fn new() -> Self {
        let directory = tempdir().unwrap();
        fs::write(
            directory.path().join("firmware with spaces.elf"),
            b"admission fixture; not firmware",
        )
        .unwrap();
        let executable = directory.path().join("qemu");
        fs::write(
            &executable,
            r#"#!/bin/sh
if [ "$1" = -machine ] && [ "$2" = help ]; then
    printf 'probe\n' >> "$FIXTURE_DIR/probes"
    case "$QUERY_MODE" in
        unsupported) printf 'virt Generic ARM\n'; exit 0 ;;
        failure) printf 'query diagnostic\n' >&2; exit 9 ;;
        overflow) printf '%0131073d\n' 0; exit 0 ;;
        timeout) printf '%s\n' "$$" > "$FIXTURE_DIR/query-pid"; exec sleep 30 ;;
    esac
    printf 'Supported machines:\ndm-mc02 Board\n'
    exit 0
fi
printf '%s\000' "$@" > "$FIXTURE_DIR/arguments"
printf '%s\n' "$$" > "$FIXTURE_DIR/native-pid"
exit "${NATIVE_EXIT:-0}"
"#,
        )
        .unwrap();
        fs::set_permissions(executable, fs::Permissions::from_mode(0o700)).unwrap();
        Self { directory }
    }

    fn path(&self, name: &str) -> std::path::PathBuf {
        self.directory.path().join(name)
    }

    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sim"));
        command
            .arg("firmware")
            .arg(self.path("firmware with spaces.elf"))
            .env("QEMU_SYSTEM_ARM", self.path("qemu"))
            .env("FIXTURE_DIR", self.directory.path())
            .env_remove("QUERY_MODE");
        command
    }

    fn arguments(&self) -> Vec<String> {
        let bytes = fs::read(self.path("arguments")).unwrap();
        bytes
            .split(|&byte| byte == 0)
            .filter(|part| !part.is_empty())
            .map(|part| String::from_utf8(part.to_vec()).unwrap())
            .collect()
    }
}

fn assert_code(output: &Output, code: i32) {
    assert_eq!(
        output.status.code(),
        Some(code),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn default_terminal_uses_environment_and_exec_preserves_process_identity_and_exit() {
    let fixture = Fixture::new();
    let child = fixture.command().env("NATIVE_EXIT", "37").spawn().unwrap();
    let launcher_pid = child.id();
    let status = child.wait_with_output().unwrap();
    assert_code(&status, 37);
    assert_eq!(
        fs::read_to_string(fixture.path("native-pid"))
            .unwrap()
            .trim(),
        launcher_pid.to_string()
    );
    let mut expected = vec![
        "-machine".into(),
        "dm-mc02".into(),
        "-kernel".into(),
        fixture
            .path("firmware with spaces.elf")
            .to_str()
            .unwrap()
            .into(),
        "-nodefaults".into(),
        "-display".into(),
        "none".into(),
        "-monitor".into(),
        "none".into(),
        "-serial".into(),
        "none".into(),
        "-serial".into(),
        "mon:stdio".into(),
    ];
    for _ in 0..8 {
        expected.extend(["-serial".to_string(), "none".to_string()]);
    }
    assert_eq!(fixture.arguments(), expected);
}

#[test]
fn explicit_binary_wins_and_all_uart_slots_preserve_cosim_and_debug_options() {
    let fixture = Fixture::new();
    let qmp = fixture.path("qmp,sock");
    let cosim = fixture.path("cosim,sock");
    for (uart, slot) in [
        ("uart1", Some(1)),
        ("uart2", Some(2)),
        ("uart3", Some(3)),
        ("uart5", Some(4)),
        ("uart7", Some(5)),
        ("uart10", Some(6)),
        ("none", None),
    ] {
        let output = fixture
            .command()
            .env("QEMU_SYSTEM_ARM", "/ignored-by-explicit-option")
            .args([
                "--qemu",
                fixture.path("qemu").to_str().unwrap(),
                "--uart",
                uart,
                "--debug",
                "--gdb-port",
                "2345",
                "--machine",
                "dm-mc02,board-profile=DM-MC02",
                "--qmp",
                qmp.to_str().unwrap(),
                "--cosim",
                cosim.to_str().unwrap(),
            ])
            .output()
            .unwrap();
        assert_code(&output, 0);
        let arguments = fixture.arguments();
        let serials: Vec<_> = arguments
            .windows(2)
            .filter(|pair| pair[0] == "-serial")
            .map(|pair| pair[1].as_str())
            .collect();
        let mut expected = [
            "chardev:cosim",
            "none",
            "none",
            "none",
            "none",
            "none",
            "none",
            "none",
            "none",
            "none",
        ];
        if let Some(slot) = slot {
            expected[slot] = "mon:stdio";
        }
        assert_eq!(serials, expected);
        assert_eq!(arguments[1], "dm-mc02,board-profile=DM-MC02");
        assert_eq!(arguments[8], if slot.is_none() { "stdio" } else { "none" });
        assert!(arguments.contains(&format!(
            "socket,id=cosim,path={}/cosim,,sock,server=on,wait=off",
            fixture.directory.path().display()
        )));
        assert!(arguments.contains(&format!(
            "unix:{}/qmp,,sock,server=on,wait=off",
            fixture.directory.path().display()
        )));
        assert_eq!(
            &arguments[arguments.len() - 3..],
            ["-S", "-gdb", "tcp:127.0.0.1:2345"]
        );
    }
}

#[test]
fn packaged_default_is_relative_to_executable_and_has_no_python_or_path_requirement() {
    let fixture = Fixture::new();
    fs::create_dir(fixture.path("bin")).unwrap();
    fs::copy(env!("CARGO_BIN_EXE_sim"), fixture.path("sim")).unwrap();
    fs::copy(fixture.path("qemu"), fixture.path("bin/qemu-system-arm")).unwrap();
    let output = Command::new(fixture.path("sim"))
        .arg("firmware")
        .arg(fixture.path("firmware with spaces.elf"))
        .env("FIXTURE_DIR", fixture.directory.path())
        .env("PATH", "/missing-path")
        .env_remove("QEMU_SYSTEM_ARM")
        .output()
        .unwrap();
    assert_code(&output, 0);
    assert!(String::from_utf8_lossy(&output.stderr).contains("bin/qemu-system-arm"));
}

#[test]
fn parser_and_admission_fail_before_querying_native_program() {
    let fixture = Fixture::new();
    for options in [
        &["--gdb-port", "1234"][..],
        &["--debug", "--gdb-port", "0"],
        &["--uart", "USART1"],
    ] {
        assert_code(&fixture.command().args(options).output().unwrap(), 2);
    }
    let alias = fixture.path("alias");
    symlink(fixture.directory.path(), &alias).unwrap();
    let occupied = fixture.path("occupied");
    symlink(fixture.path("missing-target"), &occupied).unwrap();
    let long = fixture.path(&"x".repeat(101));
    let cases = [
        vec!["--machine".into(), "none".into()],
        vec![
            "--qmp".into(),
            fixture.path("qmp").into_os_string(),
            "--cosim".into(),
            alias.join("qmp").into_os_string(),
        ],
        vec!["--qmp".into(), occupied.into_os_string()],
        vec!["--cosim".into(), long.into_os_string()],
        vec![
            "--qmp".into(),
            fixture.path("missing-parent/socket").into_os_string(),
        ],
    ];
    for options in cases {
        assert_code(&fixture.command().args(options).output().unwrap(), 1);
    }
    fs::remove_file(fixture.path("firmware with spaces.elf")).unwrap();
    assert_code(&fixture.command().output().unwrap(), 1);
    assert!(!fixture.path("probes").exists());
    assert!(!fixture.path("arguments").exists());
}

#[test]
fn backend_failures_return_78_without_attempting_firmware_execution() {
    let fixture = Fixture::new();
    for (mode, diagnostic) in [
        ("unsupported", "does not provide the dm-mc02"),
        ("failure", "query diagnostic"),
        ("overflow", "output exceeds 128 KiB"),
    ] {
        let output = fixture.command().env("QUERY_MODE", mode).output().unwrap();
        assert_code(&output, 78);
        assert!(String::from_utf8_lossy(&output.stderr).contains(diagnostic));
    }
    let output = fixture
        .command()
        .arg("--qemu")
        .arg(fixture.path("missing"))
        .output()
        .unwrap();
    assert_code(&output, 78);
    assert!(String::from_utf8_lossy(&output.stderr).contains("--qemu PATH"));
    assert!(!fixture.path("arguments").exists());
}

#[test]
fn capability_query_timeout_kills_and_reaps_native_probe() {
    let fixture = Fixture::new();
    let started = Instant::now();
    let output = fixture
        .command()
        .env("QUERY_MODE", "timeout")
        .output()
        .unwrap();
    assert_code(&output, 78);
    assert!(started.elapsed() < Duration::from_secs(8));
    assert!(String::from_utf8_lossy(&output.stderr).contains("after 5 seconds"));
    #[cfg(target_os = "linux")]
    {
        let pid = fs::read_to_string(fixture.path("query-pid")).unwrap();
        assert!(
            !Path::new("/proc").join(pid.trim()).exists(),
            "query child was not reaped"
        );
    }
    assert!(!fixture.path("arguments").exists());
}
