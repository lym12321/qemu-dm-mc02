//! Fixed RoboMaster 2026 V1.1.0 referee fixtures, independent of boards/worlds.

pub mod messages;
pub mod protocol;
pub use messages::{encode_message, Buff, GameStatus, Message, PowerHeat, RobotStatus};
pub use protocol::{decode_frame, encode_frame, Frame};
pub mod engine;
pub use engine::{
    validate_referee_events, Hit, MatchDurations, RefereeConfiguration, RefereeEngine,
    RefereeEvent, RefereeState, ScriptEvent, Shot,
};
pub mod endpoint;
pub use endpoint::RefereeUartEndpoint;
#[cfg(feature = "scene")]
pub mod config;
#[cfg(feature = "simulation")]
pub mod factory;
