pub mod qemu_can;
#[cfg(all(target_os = "linux", feature = "socketcan"))]
pub mod socketcan;
