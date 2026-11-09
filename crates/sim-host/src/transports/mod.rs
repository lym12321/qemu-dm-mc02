pub mod fixed84;
#[cfg(all(target_os = "linux", feature = "socketcan"))]
pub mod socketcan;
pub mod stream;
