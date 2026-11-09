//! Pure configuration admission; this module creates no simulation resources.

pub mod assertions;
#[cfg(feature = "scene")]
pub mod scene;
pub mod yaml;
