//! Read-only OS observations of the exact QEMU process and QMP-reported vCPU.
//! CPU seconds are cumulative; a delta of one second per wall second is one core.
use serde_json::{Value, json};
use std::io;

pub struct Sample {
    process_user_s: f64,
    process_kernel_s: f64,
    vcpu_user_s: f64,
    vcpu_kernel_s: f64,
    rss_bytes: u64,
}

impl Sample {
    pub fn json(&self) -> Value {
        json!({
            "process_user_s": self.process_user_s,
            "process_kernel_s": self.process_kernel_s,
            "vcpu_user_s": self.vcpu_user_s,
            "vcpu_kernel_s": self.vcpu_kernel_s,
            "rss_bytes": self.rss_bytes,
        })
    }
}

#[cfg(target_os = "linux")]
pub struct Observer {
    process: String,
    thread: String,
}

#[cfg(target_os = "linux")]
impl Observer {
    pub fn new(pid: u32, tid: u32) -> io::Result<Self> {
        Ok(Self {
            process: format!("/proc/{pid}/stat"),
            thread: format!("/proc/{pid}/task/{tid}/stat"),
        })
    }

    pub fn sample(&self) -> io::Result<Sample> {
        // proc_pid_stat(5): fields 14/15 are CPU ticks, field 24 is RSS pages.
        // The parenthesized comm field may itself contain spaces and ')'.
        fn stat(path: &str) -> io::Result<(f64, f64, u64)> {
            let text = std::fs::read_to_string(path)?;
            let (_, fields) = text.rsplit_once(") ").ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid proc stat comm")
            })?;
            let fields: Vec<_> = fields.split_whitespace().collect();
            let number = |index: usize| -> io::Result<u64> {
                fields
                    .get(index)
                    .and_then(|s| s.parse().ok())
                    .ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidData, "invalid proc stat field")
                    })
            };
            let hz = rustix::param::clock_ticks_per_second() as f64;
            Ok((
                number(11)? as f64 / hz,
                number(12)? as f64 / hz,
                number(21)? * rustix::param::page_size() as u64,
            ))
        }
        let (process_user_s, process_kernel_s, rss_bytes) = stat(&self.process)?;
        let (vcpu_user_s, vcpu_kernel_s, _) = stat(&self.thread)?;
        Ok(Sample {
            process_user_s,
            process_kernel_s,
            vcpu_user_s,
            vcpu_kernel_s,
            rss_bytes,
        })
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::{
        Foundation::{FILETIME, HANDLE},
        System::{
            ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
            Threading::{
                GetProcessTimes, GetThreadTimes, OpenProcess, OpenThread,
                PROCESS_QUERY_INFORMATION, PROCESS_VM_READ, THREAD_QUERY_LIMITED_INFORMATION,
            },
        },
    };

    pub struct Observer {
        process: OwnedHandle,
        thread: OwnedHandle,
    }

    fn own(handle: HANDLE) -> io::Result<OwnedHandle> {
        if handle.is_null() {
            Err(io::Error::last_os_error())
        } else {
            // SAFETY: successful OpenProcess/OpenThread returns a new owned handle.
            Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
        }
    }

    impl Observer {
        pub fn new(pid: u32, tid: u32) -> io::Result<Self> {
            // SAFETY: scalar IDs belong to the live Lab owner; query-only access,
            // non-inherited handles retained until this observation ends.
            let process =
                own(unsafe { OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, pid) })?;
            let thread = own(unsafe { OpenThread(THREAD_QUERY_LIMITED_INFORMATION, 0, tid) })?;
            Ok(Self { process, thread })
        }

        pub fn sample(&self) -> io::Result<Sample> {
            let mut creation = FILETIME::default();
            let mut exit = FILETIME::default();
            let mut kernel = FILETIME::default();
            let mut user = FILETIME::default();
            let seconds = |t: FILETIME| {
                (((t.dwHighDateTime as u64) << 32) | t.dwLowDateTime as u64) as f64 * 1e-7
            };
            // SAFETY: retained handles and initialized, correctly typed output buffers.
            if unsafe {
                GetProcessTimes(
                    self.process.as_raw_handle(),
                    &mut creation,
                    &mut exit,
                    &mut kernel,
                    &mut user,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            let process_user_s = seconds(user);
            let process_kernel_s = seconds(kernel);
            if unsafe {
                GetThreadTimes(
                    self.thread.as_raw_handle(),
                    &mut creation,
                    &mut exit,
                    &mut kernel,
                    &mut user,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            let mut memory = PROCESS_MEMORY_COUNTERS::default();
            let size = std::mem::size_of_val(&memory) as u32;
            memory.cb = size;
            if unsafe { K32GetProcessMemoryInfo(self.process.as_raw_handle(), &mut memory, size) }
                == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(Sample {
                process_user_s,
                process_kernel_s,
                vcpu_user_s: seconds(user),
                vcpu_kernel_s: seconds(kernel),
                rss_bytes: memory.WorkingSetSize as u64,
            })
        }
    }
}

#[cfg(windows)]
pub use windows::Observer;
