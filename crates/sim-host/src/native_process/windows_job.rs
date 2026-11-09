// Narrow port of process-wrap 10.0.1's suspended-spawn/resume sequence.
// Upstream: https://github.com/watchexec/process-wrap (src/windows.rs).
// Crate SHA256: 1f21b97672d2dc848e7b25701ab4535618b92f4861c13cc3f7f7bed52ad3c8da.
// Changes: windows-sys bindings, std OwnedHandle, bounded job accounting query;
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
    io,
    mem::size_of,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    process::Child,
    ptr,
};
use windows_sys::Win32::{
    Foundation::{ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE},
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
        },
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
            JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
            TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME},
    },
};

pub(super) struct Job(OwnedHandle);
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

    pub(super) fn empty(&self) -> io::Result<bool> {
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
        Ok(accounting.ActiveProcesses == 0)
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
