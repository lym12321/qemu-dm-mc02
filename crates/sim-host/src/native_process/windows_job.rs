// Narrow port of process-wrap 10.0.1's suspended-spawn/resume sequence.
// Upstream: https://github.com/watchexec/process-wrap (src/windows.rs).
// Crate SHA256: 1f21b97672d2dc848e7b25701ab4535618b92f4861c13cc3f7f7bed52ad3c8da.
// Changes: windows-sys bindings, std OwnedHandle, bounded job/process queries;
// no completion-port wrapper or unbounded wait. Selected under the MIT license:
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

use std::{
    collections::BTreeMap,
    io,
    mem::{offset_of, size_of},
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    process::Child,
    ptr,
    time::Instant,
};
use windows_sys::Win32::{
    Foundation::{
        ERROR_INVALID_PARAMETER, ERROR_MORE_DATA, ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE,
        WAIT_OBJECT_0, WAIT_TIMEOUT,
    },
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
        },
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
            JobObjectBasicAccountingInformation, JobObjectBasicProcessIdList,
            JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
            TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
            JOBOBJECT_BASIC_PROCESS_ID_LIST, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        Threading::{
            OpenProcess, OpenThread, ResumeThread, WaitForSingleObject,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE, THREAD_SUSPEND_RESUME,
        },
    },
};

pub(super) struct Job(OwnedHandle);

/// Original process handles are the completion evidence, not reusable PIDs or
/// the job's active count, which can reach zero before termination completes.
#[derive(Default)]
pub(super) struct Processes(BTreeMap<u32, OwnedHandle>);
impl Processes {
    pub(super) fn exited(&self) -> io::Result<bool> {
        for process in self.0.values() {
            // SAFETY: the owned process handle grants SYNCHRONIZE.
            match unsafe { WaitForSingleObject(process.as_raw_handle(), 0) } {
                WAIT_OBJECT_0 => (),
                WAIT_TIMEOUT => return Ok(false),
                _ => return Err(io::Error::last_os_error()),
            }
        }
        Ok(true)
    }
}

impl Job {
    pub(super) fn new() -> io::Result<Self> {
        // No name or inheritable security attributes: only this owner holds it.
        let handle = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: CreateJobObjectW returned a new owned kernel handle.
        let job = Self(unsafe { OwnedHandle::from_raw_handle(handle) });
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: the structure and handle remain valid throughout this call.
        if unsafe {
            SetInformationJobObject(
                job.0.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                ptr::from_ref(&limits).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(job)
    }

    /// The caller registers a CREATE_SUSPENDED child before this fallible call.
    pub(super) fn assign(&self, child: &Child) -> io::Result<()> {
        // SAFETY: both handles are borrowed from live owners. Suspension prevents
        // guest code spawning a descendant before it belongs to this job.
        if unsafe { AssignProcessToJobObject(self.0.as_raw_handle(), child.as_raw_handle()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    pub(super) fn terminate(&self) -> io::Result<()> {
        // SAFETY: this owner retains its non-inherited job handle.
        if unsafe { TerminateJobObject(self.0.as_raw_handle(), 1) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    pub(super) fn accounting(&self) -> io::Result<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION> {
        let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        // SAFETY: accounting is the exact output structure for this information
        // class; a successful query supplies the whole job's active count.
        if unsafe {
            QueryInformationJobObject(
                self.0.as_raw_handle(),
                JobObjectBasicAccountingInformation,
                ptr::from_mut(&mut accounting).cast(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                ptr::null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(accounting)
    }

    /// Retain live members before sending any termination request. Repeat if a
    /// process was created while querying; the caller checks this total again
    /// after termination so a creation race cannot publish cleanup success.
    pub(super) fn capture(&self, processes: &mut Processes, deadline: Instant) -> io::Result<u32> {
        let header = offset_of!(JOBOBJECT_BASIC_PROCESS_ID_LIST, ProcessIdList);
        let mut capacity = 16;
        loop {
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "native job process capture deadline expired",
                ));
            }
            let total = self.accounting()?.TotalProcesses;
            let length = header + capacity * size_of::<usize>();
            let bytes = u32::try_from(length)
                .map_err(|_| io::Error::other("native job process list is too large"))?;
            // usize storage aligns the variable-length Windows output structure.
            let mut buffer = vec![0usize; length.div_ceil(size_of::<usize>())];
            let list = buffer
                .as_mut_ptr()
                .cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>();
            // SAFETY: buffer is aligned and contains exactly bytes writable bytes.
            let queried = unsafe {
                QueryInformationJobObject(
                    self.0.as_raw_handle(),
                    JobObjectBasicProcessIdList,
                    list.cast(),
                    bytes,
                    ptr::null_mut(),
                )
            };
            if queried == 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(ERROR_MORE_DATA as i32) {
                    return Err(error);
                }
                // ERROR_MORE_DATA supplies the number needed in the fixed header.
                capacity = (unsafe { (*list).NumberOfAssignedProcesses } as usize)
                    .max(capacity.saturating_mul(2));
                continue;
            }
            // SAFETY: a successful query initialized the fixed header.
            let count = unsafe { (*list).NumberOfProcessIdsInList } as usize;
            if count > capacity {
                return Err(io::Error::other(
                    "native job returned an invalid process list",
                ));
            }
            // SAFETY: the successful query returned count entries in this buffer.
            let pids =
                unsafe { std::slice::from_raw_parts(buffer.as_ptr().byte_add(header), count) };
            for &pid in pids {
                let pid = u32::try_from(pid)
                    .map_err(|_| io::Error::other("native job returned an invalid process ID"))?;
                if processes.0.contains_key(&pid) {
                    continue;
                }
                // SAFETY: OpenProcess returns a new handle with only query/wait access.
                let raw = unsafe {
                    OpenProcess(
                        PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                        0,
                        pid,
                    )
                };
                if raw.is_null() {
                    let error = io::Error::last_os_error();
                    if error.raw_os_error() == Some(ERROR_INVALID_PARAMETER as i32) {
                        // The original process object has already been destroyed.
                        continue;
                    }
                    return Err(error);
                }
                // SAFETY: OpenProcess returned a new valid owned kernel handle.
                let process = unsafe { OwnedHandle::from_raw_handle(raw) };
                let mut member = 0;
                // SAFETY: both handles remain live and process grants query access.
                if unsafe {
                    IsProcessInJob(process.as_raw_handle(), self.0.as_raw_handle(), &mut member)
                } == 0
                {
                    return Err(io::Error::last_os_error());
                }
                if member == 0 {
                    // The queried process may have exited, or its PID may now
                    // identify a foreign process. Never terminate that identity.
                    if unsafe { WaitForSingleObject(process.as_raw_handle(), 0) } == WAIT_OBJECT_0 {
                        continue;
                    }
                    return Err(io::Error::other(
                        "native job process identity changed during capture",
                    ));
                }
                processes.0.insert(pid, process);
            }
            if self.accounting()?.TotalProcesses == total {
                return Ok(total);
            }
        }
    }
}

/// Resume the initially suspended child using the documented ToolHelp APIs.
/// std::process::Child retains the process handle but hides its primary thread.
pub(super) fn resume(child: &Child) -> io::Result<()> {
    // SAFETY: no pointers are passed; a successful result is a new owned handle.
    let handle = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if handle == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: CreateToolhelp32Snapshot returned a new valid handle.
    let snapshot = unsafe { OwnedHandle::from_raw_handle(handle) };
    let mut entry = THREADENTRY32 {
        dwSize: size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    // SAFETY: entry is the properly sized output structure and snapshot is live.
    if unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } == 0 {
        return Err(io::Error::last_os_error());
    }
    loop {
        if entry.th32OwnerProcessID == child.id() {
            // SAFETY: OpenThread returns a new handle to the identified thread.
            let handle = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            // SAFETY: OpenThread returned a valid owned handle.
            let thread = unsafe { OwnedHandle::from_raw_handle(handle) };
            // The CREATE_SUSPENDED child has one initial suspended thread. Stop
            // after resuming it, so a child cannot race enumeration by creating
            // additional threads once its application starts.
            // SAFETY: thread grants THREAD_SUSPEND_RESUME and remains live.
            let previous = unsafe { ResumeThread(thread.as_raw_handle()) };
            return match previous {
                u32::MAX => Err(io::Error::last_os_error()),
                1 => Ok(()),
                _ => Err(io::Error::other(
                    "native initial thread was not singly suspended",
                )),
            };
        }
        // SAFETY: same live snapshot and properly initialized output structure.
        if unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } == 0 {
            let error = io::Error::last_os_error();
            return Err(
                if error.raw_os_error() == Some(ERROR_NO_MORE_FILES as i32) {
                    io::Error::other("native initial thread was absent from the snapshot")
                } else {
                    error
                },
            );
        }
    }
}
