#[cfg(feature = "ros2")]
#[path = "build_support/ros2.rs"]
mod ros2;

fn main() {
    #[cfg(feature = "ros2")]
    ros2::build();
    #[cfg(feature = "gazebo")]
    {
        let source = "../../sim/physics/gazebo_native";
        for file in [
            "world.cc",
            "resources.hh",
            "render_types.hh",
            "rendering.hh",
            "world_bridge.hh",
            "rust_bridge.inc",
        ] {
            println!("cargo:rerun-if-changed={source}/{file}");
        }
        println!("cargo:rerun-if-changed=src/physics/gazebo/ffi.rs");
        let mut build = cxx_build::bridge("src/physics/gazebo/ffi.rs");
        build
            .file(format!("{source}/world.cc"))
            .include(source)
            .std("c++17")
            .warnings_into_errors(true);
        let mut dependencies = vec![("gz-sim8", "8.15.0"), ("gz-sensors8-imu", "8.2.2")];
        if cfg!(feature = "gazebo-rendering") {
            build.define("DM_GAZEBO_RENDERING", None);
            dependencies.extend([("gz-transport13", "13.6.0"), ("gz-msgs10", "10.4.0")]);
        }
        for (package, version) in dependencies {
            let library = pkg_config::Config::new()
                .exactly_version(version)
                .probe(package)
                .unwrap_or_else(|error| {
                    panic!(
                        "Gazebo feature requires {package}={version} via PKG_CONFIG_PATH: {error}"
                    )
                });
            build.includes(library.include_paths);
            for (name, value) in library.defines {
                build.define(&name, value.as_deref());
            }
        }
        build.compile("dm_gazebo_native");
    }
    #[cfg(feature = "cosim-wire")]
    {
        // The existing portable C codecs remain the canonical wire owner.
        let source = "../../cosim";
        for name in [
            "dm_mc02_wire.h",
            "dm_mc02_wire.c",
            "dm_mc02_v2_wire.h",
            "dm_mc02_v2_wire.c",
            "dm_mc02_v2_payload.h",
            "dm_mc02_v2_payload.c",
        ] {
            println!("cargo:rerun-if-changed={source}/{name}");
        }
        cc::Build::new()
            .include(source)
            .file(format!("{source}/dm_mc02_wire.c"))
            .file(format!("{source}/dm_mc02_v2_wire.c"))
            .file(format!("{source}/dm_mc02_v2_payload.c"))
            .compile("dm_mc02_wire");
    }
}
