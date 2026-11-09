//! Protocol-neutral topology, completed observations and one simulation owner.
//!
//! Adapters supply owned configuration, input and observation values. This
//! crate neither interprets a transport nor starts a backend or physics clock.

mod data;
mod runtime;
mod topology;

pub use data::*;
pub use runtime::*;
pub use topology::*;
