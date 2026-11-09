//! Compiled host simulation adapters, native resource owners and applications.
//!
//! The protocol-neutral core owns simulation time. Adapters preserve native
//! source timestamps and make process, transport and cleanup ownership explicit.

#[cfg(feature = "scene")]
pub mod apps;
pub mod assertions;
pub mod board_launch;
#[cfg(feature = "boards")]
pub mod boards;
#[cfg(all(target_os = "linux", feature = "scene"))]
pub mod bundle;
pub mod can;
#[cfg(feature = "yaml")]
pub mod config;
#[cfg(all(feature = "simulation", feature = "cosim-wire"))]
pub mod cosim_session;
#[cfg(any(
    feature = "devices",
    feature = "machine",
    feature = "referee",
    feature = "worker"
))]
pub mod devices;
pub mod endpoints;
#[cfg(feature = "scene")]
pub mod event_json;
#[cfg(feature = "simulation")]
pub mod fabric;
#[cfg(all(any(target_os = "linux", windows), feature = "lab"))]
pub mod lab;
pub mod launch;
#[cfg(all(target_os = "linux", feature = "machine"))]
pub mod machine;
#[cfg(all(any(target_os = "linux", windows), feature = "native-process"))]
pub mod native_process;
#[cfg(feature = "physics")]
pub mod physics;
#[cfg(all(target_os = "linux", feature = "host-processes"))]
pub mod processes;
#[cfg(all(target_os = "linux", feature = "machine"))]
pub mod profile;
#[cfg(feature = "qmp")]
pub mod qmp;
#[cfg(feature = "ros2")]
pub mod ros2;
#[cfg(feature = "simulation")]
pub mod runtime;
#[cfg(any(feature = "simulation", feature = "physics"))]
pub mod signals;
#[cfg(feature = "cosim-wire")]
pub mod step_wire;
#[cfg(feature = "trace")]
pub mod trace;
pub mod transports;
#[cfg(feature = "lab")]
pub mod usbip;
#[cfg(feature = "simulation")]
pub mod worker;

use std::{fmt, io, sync::Arc};

/// Errors retain identity so a terminal owner can preserve its first failure.
#[derive(Debug)]
pub enum Error {
    Admission(&'static str),
    State(&'static str),
    Capacity(&'static str),
    Io(io::Error),
    Callback(String),
}

pub type Failure = Arc<Error>;
pub type Result<T> = std::result::Result<T, Failure>;

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Admission(message) | Self::State(message) | Self::Capacity(message) => {
                f.write_str(message)
            }
            Self::Io(error) => error.fmt(f),
            Self::Callback(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}
