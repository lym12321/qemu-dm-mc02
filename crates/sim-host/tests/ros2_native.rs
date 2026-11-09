#![cfg(feature = "ros2")]
use sim_host::ros2::{Ros2Engine, Ros2Options, Ros2Transport};
use sim_host::worker::motor_policy::MitCommand;
use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};

struct Peer {
    child: Child,
    output: BufReader<std::process::ChildStdout>,
}
impl Peer {
    fn start(prefix: &str) -> Self {
        let path = std::env::var_os("SIM_ROS2_TEST_PEER")
            .expect("SIM_ROS2_TEST_PEER points to the CMake dm_ros2_test_peer executable");
        let mut child = Command::new(path)
            .arg(prefix)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        let mut peer = Self { child, output };
        assert_eq!(peer.line(), "READY");
        peer
    }
    fn line(&mut self) -> String {
        let mut value = String::new();
        assert!(
            self.output.read_line(&mut value).unwrap() > 0,
            "native ROS peer exited"
        );
        value.trim().into()
    }
    fn request(&mut self, command: &str) -> String {
        writeln!(self.child.stdin.as_mut().unwrap(), "{command}").unwrap();
        self.child.stdin.as_mut().unwrap().flush().unwrap();
        self.line()
    }
    fn status(&mut self, expected: &str) {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let value = self.request("status");
            if value == expected {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "last peer observation {value}; expected {expected}"
            );
        }
    }
    fn close(mut self) {
        writeln!(self.child.stdin.as_mut().unwrap(), "quit").unwrap();
        assert!(self.child.wait().unwrap().success());
    }
}
impl Drop for Peer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
fn options(reset: bool) -> (String, Ros2Options) {
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let prefix = format!(
        "/rust_ros_{}_{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    );
    let value = Ros2Options {
        motor_count: 2,
        motor_topic: format!("{prefix}/motors"),
        imu_topic: format!("{prefix}/imu"),
        joint_state_topic: format!("{prefix}/joints"),
        node_name: format!(
            "rust_ros_{}_{}",
            std::process::id(),
            NEXT.load(Ordering::Relaxed)
        ),
        reset_service: if reset {
            format!("{prefix}/reset")
        } else {
            String::new()
        },
    };
    (prefix, value)
}
fn until(transport: &mut Ros2Transport, condition: impl Fn(&sim_host::ros2::Ros2Snapshot) -> bool) {
    let deadline = Instant::now() + Duration::from_secs(3);
    while !condition(&transport.sample().unwrap()) {
        assert!(Instant::now() < deadline, "DDS observation timed out");
        transport.pump(Duration::from_millis(1)).unwrap();
    }
}

#[test]
fn options_reject_before_native_context_creation() {
    let (_, mut value) = options(false);
    value.motor_count = 0;
    assert!(value.validate().is_ok());
    value.motor_count = 2;
    value.imu_topic = "a\0b".into();
    assert!(value.validate().is_err());
}

#[test]
#[ignore = "requires ROS2 Jazzy and SIM_ROS2_TEST_PEER"]
fn native_transport_preserves_indexed_si_stamps_reset_and_failure() {
    let (prefix, value) = options(true);
    let mut transport = Ros2Transport::new(&value).unwrap();
    assert!(!std::fs::read_to_string("/proc/self/maps")
        .unwrap()
        .contains("libpython"));
    let mut peer = Peer::start(&prefix);
    assert_eq!(peer.request("sample 7"), "SENT");
    until(&mut transport, |s| {
        s.source_time_ns == Some(7_000_000_123) && s.position_rad[0] == 0.1
    });
    let sample = transport.sample().unwrap();
    assert_eq!(sample.imu.gyro_rad_s, [7.0, -2.0, 3.0]);
    assert_eq!(sample.imu.accel_m_s2, [4.0, -5.0, 9.80665]);
    assert_eq!(sample.position_rad, [0.1, 0.2]);
    assert_eq!(sample.velocity_rad_s, [1.1, 1.2]);
    assert_eq!(sample.effort_nm, [Some(2.1), None]);
    assert_eq!(peer.request("short"), "SENT");
    until(&mut transport, |s| s.position_rad[0] == 0.7);
    let sample = transport.sample().unwrap();
    assert_eq!(
        (sample.position_rad, sample.velocity_rad_s, sample.effort_nm),
        (vec![0.7, 0.0], vec![0.0, 0.0], vec![None, None])
    );
    assert_eq!(peer.request("badstamp 9"), "SENT");
    until(&mut transport, |s| s.imu.gyro_rad_s[0] == 9.0);
    assert_eq!(
        transport.sample().unwrap().source_time_ns,
        Some(7_000_000_123)
    );
    let held = transport.step(Duration::from_millis(1), true).unwrap();
    assert_eq!(held.source_time_ns, Some(7_000_000_123));
    transport.publish_efforts(&[2.0, -3.0]).unwrap();
    peer.status("STATUS 0 2 -3");
    transport.reset(Duration::from_secs(2)).unwrap();
    peer.status("STATUS 1 0 0");
    assert_eq!(transport.sample().unwrap().source_time_ns, None);
    assert_eq!(peer.request("nan 10"), "SENT");
    let deadline = Instant::now() + Duration::from_secs(3);
    let failure = loop {
        if let Err(error) = transport.pump(Duration::from_millis(1)) {
            break error;
        }
        assert!(Instant::now() < deadline);
    };
    assert!(failure.to_string().contains("nonfinite"));
    assert!(std::sync::Arc::ptr_eq(
        &failure,
        &transport.publish_efforts(&[0.0, 0.0]).unwrap_err()
    ));
    transport.reset(Duration::from_secs(2)).unwrap();
    peer.status("STATUS 2 0 0");
    transport.close().unwrap();
    peer.close();
}

#[test]
#[ignore = "requires ROS2 Jazzy and SIM_ROS2_TEST_PEER"]
fn actual_engine_consumes_shared_motor_policy_and_fresh_native_imu() {
    let (prefix, value) = options(false);
    let mut engine = Ros2Engine::new(&value, 4.0, true, Duration::from_secs(2)).unwrap();
    let mut peer = Peer::start(&prefix);
    assert_eq!(peer.request("sample 3"), "SENT");
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let sample = engine.step(Duration::from_millis(2)).unwrap();
        if sample.source_time_ns == Some(3_000_000_123) && sample.position_rad[0] == 0.1 {
            break;
        }
        assert!(Instant::now() < deadline);
    }
    engine.set_motor(1, 7.0).unwrap();
    peer.status("STATUS 0 0 0");
    engine.set_motor_enabled(1, true).unwrap();
    peer.status("STATUS 0 0 4");
    engine.set_motor_enabled(0, true).unwrap();
    engine
        .set_motor_dm(
            0,
            MitCommand {
                position: 0.5,
                speed: 0.0,
                kp: 2.0,
                kd: 0.0,
                torque: 0.2,
            },
        )
        .unwrap();
    peer.status("STATUS 0 1 4");
    assert_eq!(engine.motor_feedback(0).unwrap(), (0.1, 1.1, 2.1));
    assert_eq!(engine.motor_feedback(1).unwrap(), (0.2, 1.2, 4.0));
    engine.set_motor_enabled(0, false).unwrap();
    engine.set_motor_enabled(0, true).unwrap();
    peer.status("STATUS 0 0 4");
    engine.reset().unwrap();
    peer.status("STATUS 0 0 0");
    engine.close().unwrap();
    peer.close();
}

#[test]
#[ignore = "requires ROS2 Jazzy"]
fn absent_reset_service_times_out_and_cleanup_does_not_shutdown_other_context() {
    let (_, first) = options(true);
    let (_, second) = options(false);
    let mut broken = Ros2Transport::new(&first).unwrap();
    let mut survivor = Ros2Transport::new(&second).unwrap();
    let start = Instant::now();
    let error = broken.reset(Duration::from_millis(30)).unwrap_err();
    assert!(error.to_string().contains("timed out"));
    assert!(start.elapsed() < Duration::from_secs(1));
    let retry = broken.reset(Duration::from_millis(1)).unwrap_err();
    assert!(std::sync::Arc::ptr_eq(&error, &retry));
    broken.close().unwrap();
    survivor.pump(Duration::ZERO).unwrap();
    survivor.publish_efforts(&[1.0, 2.0]).unwrap();
    survivor.close().unwrap();
}
