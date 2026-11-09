//! Fixed DM-MC02 native profile shared by stepped and free-running consumers.
use std::{ffi::OsString, path::Path};
pub const UARTS: &[&str] = &["uart1", "uart2", "uart3", "uart5", "uart7", "uart10"];
pub const CANS: &[&str] = &["can1", "can2", "can3"];
pub const SERIAL_SLOTS: &[&str] = &[
    "cosim", "uart1", "uart2", "uart3", "uart5", "uart7", "uart10", "can1", "can2", "can3", "usb",
];
pub fn machine_arguments(firmware: &Path) -> Vec<OsString> {
    vec![
        "-machine".into(),
        "dm-mc02,fdcan-host-ack=on".into(),
        "-kernel".into(),
        firmware.as_os_str().to_owned(),
    ]
}
