//! Bounded, query-only QMP IO. A reply acknowledges an RPC, never a virtual cut.
//!
//! The pinned qapi schema supplies standard commands. Project commands below
//! follow the canonical native QEMU 8.2.2 run-state.json extension.

pub use qapi::futures::{QmpPreparedCommand as PreparedCommand, QMP_MAX_FRAME_LENGTH};
pub use qapi::qmp as commands;

use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize)]
pub struct QueryVirtualExecution {}

impl qapi::Command for QueryVirtualExecution {
    type Ok = VirtualExecutionInfo;
    const NAME: &'static str = "x-query-virtual-execution";
    const ALLOW_OOB: bool = false;
}
impl commands::QmpCommand for QueryVirtualExecution {}

#[derive(Debug, Serialize)]
pub struct RunToVirtualTime {
    #[serde(rename = "target-ns")]
    pub target_ns: i64,
}

impl qapi::Command for RunToVirtualTime {
    type Ok = qapi::Empty;
    const NAME: &'static str = "x-run-to-virtual-time";
    const ALLOW_OOB: bool = false;
}
impl commands::QmpCommand for RunToVirtualTime {}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum VirtualExecutionState {
    Idle,
    Running,
    Reached,
    Cancelled,
}

#[derive(Debug, Deserialize)]
pub struct VirtualExecutionInfo {
    #[serde(rename = "current-ns")]
    pub current_ns: i64,
    pub state: VirtualExecutionState,
    #[serde(rename = "target-ns")]
    pub target_ns: Option<i64>,
}

#[cfg(any(unix, windows))]
mod session;
#[cfg(any(unix, windows))]
pub use session::*;
