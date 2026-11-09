//! Compiled external co-simulation worker over shared host boundaries.
#[cfg(all(feature = "worker", feature = "devices"))]
pub mod can_graph;
#[cfg(feature = "physics")]
pub mod engine;
#[cfg(feature = "physics")]
pub mod graph;
pub mod motor_policy;
pub mod motor_protocol;
#[cfg(all(target_os = "linux", feature = "worker"))]
mod run;
#[cfg(all(target_os = "linux", feature = "worker", feature = "devices"))]
pub use run::run_with_can_observer;
#[cfg(all(target_os = "linux", feature = "worker"))]
pub use run::{run, run_with_engine, Protocol, WorkerOptions, WorkerReport};
