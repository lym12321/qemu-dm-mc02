use std::{env, fs, path::PathBuf, process::Command};

fn run(command: &mut Command) {
    let output = command.output().expect("start native ROS2 build tool");
    assert!(
        output.status.success(),
        "ROS2 native build failed: {}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

pub fn build() {
    assert_eq!(
        env::var("CARGO_CFG_TARGET_OS").as_deref(),
        Ok("linux"),
        "the ROS2 adapter requires Linux ROS2 Jazzy"
    );
    for file in ["CMakeLists.txt", "ros_transport.hh", "ros_transport.cc"] {
        println!("cargo:rerun-if-changed=native/ros2/{file}");
    }
    println!("cargo:rerun-if-changed=src/ros2/ffi.rs");
    println!("cargo:rerun-if-changed=build_support/ros2.rs");
    println!("cargo:rerun-if-env-changed=CMAKE_PREFIX_PATH");
    println!("cargo:rerun-if-env-changed=AMENT_PREFIX_PATH");
    println!("cargo:rerun-if-env-changed=PYTHONPATH");
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR"));
    cxx_build::bridge("src/ros2/ffi.rs")
        .include("native/ros2")
        .std("c++17")
        .warnings_into_errors(true)
        .compile("dm_ros2_cxx");
    let native = out.join("ros2-native");
    run(Command::new("cmake")
        .args(["-S", "native/ros2", "-G", "Unix Makefiles", "-B"])
        .arg(&native)
        .arg(format!(
            "-DCXX_INCLUDE={}",
            out.join("cxxbridge/include").display()
        )));
    run(Command::new("cmake").arg("--build").arg(&native).args([
        "--target",
        "dm_ros2_transport",
        "--parallel",
        "2",
    ]));
    println!("cargo:rustc-link-search=native={}", native.display());
    println!("cargo:rustc-link-lib=static=dm_ros2_transport");
    let link = fs::read_to_string(native.join("CMakeFiles/dm_ros2_link_probe.dir/link.txt"))
        .expect("CMake ROS2 resolved target link command");
    // The pinned native SDK exposes canonical paths without shell escaping.
    // Reject unsupported paths rather than interpreting CMake output as a shell.
    assert!(
        !link.contains(['\'', '"', '\\']),
        "unsupported ROS2 SDK linker path"
    );
    for argument in link.split_whitespace() {
        if argument.starts_with("-Wl,")
            || argument.starts_with("-l")
            || argument.starts_with("-L")
            || (argument.starts_with('/') && argument.contains(".so"))
        {
            println!("cargo:rustc-link-arg={argument}");
        }
    }
    println!("cargo:rustc-env=SIM_ROS2_NATIVE_BUILD={}", native.display());
}
