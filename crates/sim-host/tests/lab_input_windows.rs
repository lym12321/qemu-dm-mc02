#![cfg(all(windows, feature = "lab"))]

use sim_host::lab::{
    command::Lines,
    input::{ready, Input},
};
use std::{
    fs::OpenOptions,
    io::{self, Write},
    os::windows::io::{AsHandle, AsRawHandle},
    process::{Command, Stdio},
    time::Duration,
};
use wait_timeout::ChildExt;
use windows_sys::Win32::System::Console::{
    AllocConsole, FlushConsoleInputBuffer, FreeConsole, GetConsoleMode, SetConsoleMode,
    WriteConsoleInputW, ENABLE_ECHO_INPUT, INPUT_RECORD, INPUT_RECORD_0, KEY_EVENT,
    KEY_EVENT_RECORD, KEY_EVENT_RECORD_0,
};

// AllocConsole/FreeConsole are process-global, so all native console assertions
// run in one isolated child. Other tests and the invoking terminal keep theirs.
#[test]
fn borrowed_pipe_and_native_console_preserve_stream_and_modes() {
    const CHILD: &str = "SIM_TEST_NATIVE_CONSOLE_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "borrowed_pipe_and_native_console_preserve_stream_and_modes",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .stdin(Stdio::null())
            .spawn()
            .unwrap();
        let status = match child.wait_timeout(Duration::from_secs(15)) {
            Ok(Some(status)) => status,
            other => {
                let _ = child.kill();
                let _ = child.wait();
                panic!("native input test failed its host wait: {other:?}");
            }
        };
        assert!(status.success());
        return;
    }

    pipe();
    unsafe { FreeConsole() };
    assert_ne!(
        unsafe { AllocConsole() },
        0,
        "{}",
        io::Error::last_os_error()
    );
    struct Console;
    impl Drop for Console {
        fn drop(&mut self) {
            unsafe { FreeConsole() };
        }
    }
    let _console = Console;
    let handle = OpenOptions::new()
        .read(true)
        .write(true)
        .open("CONIN$")
        .unwrap();
    assert_ne!(
        unsafe { FlushConsoleInputBuffer(handle.as_raw_handle()) },
        0
    );
    let mut mode = 0;
    assert_ne!(
        unsafe { GetConsoleMode(handle.as_raw_handle(), &mut mode) },
        0
    );
    mode |= ENABLE_ECHO_INPUT;
    assert_ne!(unsafe { SetConsoleMode(handle.as_raw_handle(), mode) }, 0);
    {
        let mut input = Input::new(handle.as_handle()).unwrap();
        let mut output = Vec::new();
        let mut bytes = [0; 4096];
        assert!(!ready(handle.as_handle()).unwrap());
        assert_eq!(input.read(&mut bytes, &mut output).unwrap(), None);

        let mut key_up = key(b'x'.into(), 1);
        key_up.Event.KeyEvent.bKeyDown = 0;
        send(&handle, &[key_up, key(b'a'.into(), 1)]);
        assert!(ready(handle.as_handle()).unwrap());
        assert_eq!(input.read(&mut bytes, &mut output).unwrap(), None);
        send(
            &handle,
            &[
                key(0xd83d, 1),
                key(0xde80, 1),
                key(8, 1),
                key(0xd83d, 1),
                key(0xde80, 1),
                key(13, 1),
            ],
        );
        let count = input.read(&mut bytes, &mut output).unwrap().unwrap();
        assert_eq!(&bytes[..count], "a🚀\n".as_bytes());
        assert_eq!(output, "a🚀\u{8} \u{8}🚀\r\n".as_bytes());

        let mut lines = Lines::default();
        send(&handle, &[key(b'x'.into(), 4096), key(13, 1)]);
        let line = next_line(&mut input, &mut lines, &mut output).unwrap();
        assert_eq!(line, vec![b'x'; 4096]);

        send(
            &handle,
            &[
                key(b'x'.into(), 4097),
                key(13, 1),
                key(b'h'.into(), 1),
                key(b'e'.into(), 1),
                key(b'l'.into(), 1),
                key(b'p'.into(), 1),
                key(13, 1),
            ],
        );
        assert_eq!(
            next_line(&mut input, &mut lines, &mut output),
            Err("command line exceeds 4096 bytes")
        );
        assert_eq!(
            next_line(&mut input, &mut lines, &mut output).unwrap(),
            b"help"
        );
        send(&handle, &[key(26, 1)]);
        assert_eq!(input.read(&mut bytes, &mut output).unwrap(), Some(0));
    }
    let mut after = 0;
    assert_ne!(
        unsafe { GetConsoleMode(handle.as_raw_handle(), &mut after) },
        0
    );
    assert_eq!(after, mode, "borrowed console modes changed");
}

fn pipe() {
    let (reader, mut writer) = io::pipe().unwrap();
    let mut output = Vec::new();
    let mut bytes = [0; 4096];
    {
        let mut input = Input::new(reader.as_handle()).unwrap();
        assert!(!ready(reader.as_handle()).unwrap());
        assert_eq!(input.read(&mut bytes, &mut output).unwrap(), None);
        let text = b"uart uart1 \0\xe4\xb8\xad\r\n";
        writer.write_all(text).unwrap();
        assert!(ready(reader.as_handle()).unwrap());
        let count = input.read(&mut bytes, &mut output).unwrap().unwrap();
        assert_eq!(&bytes[..count], text);
        writer.write_all(&[b'x'; 4096]).unwrap();
        assert_eq!(input.read(&mut bytes, &mut output).unwrap(), Some(4096));
    }
    // Dropping the adapter leaves the caller's handle usable.
    let mut input = Input::new(reader.as_handle()).unwrap();
    drop(writer);
    assert!(ready(reader.as_handle()).unwrap());
    assert_eq!(input.read(&mut bytes, &mut output).unwrap(), Some(0));
    let file = tempfile::tempfile().unwrap();
    assert!(
        matches!(Input::new(file.as_handle()), Err(error) if error.kind() == io::ErrorKind::InvalidInput)
    );
}

fn key(unit: u16, repeat: u16) -> INPUT_RECORD {
    INPUT_RECORD {
        EventType: KEY_EVENT as u16,
        Event: INPUT_RECORD_0 {
            KeyEvent: KEY_EVENT_RECORD {
                bKeyDown: 1,
                wRepeatCount: repeat,
                uChar: KEY_EVENT_RECORD_0 { UnicodeChar: unit },
                ..Default::default()
            },
        },
    }
}

fn send(handle: &std::fs::File, events: &[INPUT_RECORD]) {
    let mut written = 0;
    assert_ne!(
        unsafe {
            WriteConsoleInputW(
                handle.as_raw_handle(),
                events.as_ptr(),
                events.len() as u32,
                &mut written,
            )
        },
        0,
        "{}",
        io::Error::last_os_error()
    );
    assert_eq!(written as usize, events.len());
}

fn next_line(
    input: &mut Input<'_>,
    lines: &mut Lines,
    output: &mut Vec<u8>,
) -> Result<Vec<u8>, &'static str> {
    let mut bytes = [0; 4096];
    // The fixtures contain < 10,000 UTF-16 units; 64 bounded pumps suffice.
    for _ in 0..64 {
        if let Some(count) = input.read(&mut bytes, output).unwrap() {
            lines.feed(&bytes[..count]);
        }
        if let Some(line) = lines.next_line() {
            return line;
        }
    }
    panic!("queued console records did not produce a complete line");
}
