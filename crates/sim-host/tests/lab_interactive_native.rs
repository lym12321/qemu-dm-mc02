#![cfg(all(target_os = "linux", feature = "lab"))]
use serde_json::Value;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::Duration,
};
use wait_timeout::ChildExt;

struct Console {
    child: Child,
    input: Option<ChildStdin>,
    lines: Receiver<String>,
    reader: Option<thread::JoinHandle<()>>,
    stderr: Option<thread::JoinHandle<String>>,
}
impl Console {
    fn start(asset: &str, extra: &[&str], qmp: &std::path::Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_sim"))
            .args(["lab", "interactive", "--elf"])
            .arg(std::env::var_os(asset).expect(asset))
            .arg("--qemu")
            .arg(std::env::var_os("SIM_NATIVE_QEMU").unwrap())
            .args(["--timeout", "5", "--startup-timeout", "3", "--qmp"])
            .arg(qmp)
            .args(extra)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let input = child.stdin.take();
        let stdout = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let (sender, lines) = mpsc::channel();
        let reader = thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let line = line.unwrap();
                eprintln!("console: {line}");
                if sender.send(line).is_err() {
                    break;
                }
            }
        });
        let stderr = thread::spawn(move || {
            let mut text = String::new();
            stderr.read_to_string(&mut text).unwrap();
            text
        });
        Self {
            child,
            input,
            lines,
            reader: Some(reader),
            stderr: Some(stderr),
        }
    }
    fn send(&mut self, bytes: &[u8]) {
        self.input.as_mut().unwrap().write_all(bytes).unwrap();
    }
    fn until(&self, contains: &str) -> String {
        loop {
            let line = self
                .lines
                .recv_timeout(Duration::from_secs(6))
                .unwrap_or_else(|error| panic!("waiting for {contains:?}: {error}"));
            if line.contains(contains) {
                return line;
            }
        }
    }
    fn uart_contains(&self, expected: &str) {
        let mut text = String::new();
        loop {
            let line = self
                .lines
                .recv_timeout(Duration::from_secs(6))
                .unwrap_or_else(|error| {
                    panic!("waiting for UART {expected:?}, got {text:?}: {error}")
                });
            if let Some((_, bytes)) = line.split_once(" b\"") {
                text.push_str(bytes.strip_suffix('"').unwrap());
                if text.contains(expected) {
                    return;
                }
            }
        }
    }
    fn native_pid(&self) -> u32 {
        let children =
            fs::read_to_string(format!("/proc/{0}/task/{0}/children", self.child.id())).unwrap();
        let pids: Vec<_> = children.split_whitespace().collect();
        assert_eq!(pids.len(), 1, "one native process owner");
        pids[0].parse().unwrap()
    }
    fn finish(mut self, pid: u32, qmp: &std::path::Path) {
        self.send(b"quit");
        self.input.take(); // Final unterminated EOF is one command.
        self.until("Lab finished:");
        assert!(
            !PathBuf::from(format!("/proc/{pid}")).exists(),
            "success preceded native cleanup"
        );
        assert!(!qmp.exists());
        assert!(self
            .child
            .wait_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap()
            .success());
        self.reader.take().unwrap().join().unwrap();
        let stderr = self.stderr.take().unwrap().join().unwrap();
        assert!(stderr.is_empty(), "{stderr}");
    }
}
impl Drop for Console {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = Command::new("kill")
                .args(["-TERM", &self.child.id().to_string()])
                .status();
            if self
                .child
                .wait_timeout(Duration::from_secs(3))
                .ok()
                .flatten()
                .is_none()
            {
                let _ = self.child.kill();
                let _ = self.child.wait();
            }
        }
    }
}

#[test]
fn regular_input_is_rejected_before_firmware_or_native_resolution() {
    let file = tempfile::tempfile().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_sim"))
        .args([
            "lab",
            "interactive",
            "--elf",
            "/no/firmware",
            "--qemu",
            "/no/qemu",
        ])
        .stdin(file)
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("regular files are unsupported"));
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ELF"]
fn actual_console_pipe_key_native_state_long_line_recovery_and_cleanup() {
    let directory = tempfile::tempdir().unwrap();
    let qmp = directory.path().join("external.qmp");
    let mut console = Console::start("SIM_NATIVE_ELF", &[], &qmp);
    console.uart_contains("SIM_READY minimal-board");
    let pid = console.native_pid();
    console.send(b"key pre");
    console.send(b"ss\n");
    console.uart_contains("KEY pressed B0=1");
    console.send(b"state\n");
    let snapshot: Value = serde_json::from_str(&console.until("\"schema\":")).unwrap();
    assert!(sim_host::boards::digital_level("gpio.B0", &snapshot).unwrap());
    console.send(&[b'x'; 4096]);
    console.send(b"x");
    console.until("command line exceeds 4096 bytes");
    console.send(b"discard suffix\nvin 24000\nstate\n");
    let snapshot: Value = serde_json::from_str(&console.until("\"schema\":")).unwrap();
    assert_eq!(snapshot["vin_mv"], 24000);
    console.finish(pid, &qmp);
}

#[test]
#[ignore = "requires SIM_NATIVE_QEMU and SIM_NATIVE_ADC_ELF"]
fn actual_paused_adc_error_allows_cont_then_two_real_guest_reads() {
    let directory = tempfile::tempdir().unwrap();
    let qmp = directory.path().join("external.qmp");
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port().to_string();
    drop(listener);
    let mut console = Console::start(
        "SIM_NATIVE_ADC_ELF",
        &["--adc", "--paused", "--gdb-port", &port],
        &qmp,
    );
    console.until("Lab connected");
    let pid = console.native_pid();
    console.send(b"adc ch4 raw 1\ncont\n");
    console.until("requires running QEMU; use cont first");
    console.uart_contains("LAB_READY");
    console.send(b"adc ch4 raw 12345\n");
    console.uart_contains("ADC=12345");
    console.send(b"adc ch4 voltage_uv 1650000\n");
    console.uart_contains("ADC=32768");
    console.finish(pid, &qmp);
}
