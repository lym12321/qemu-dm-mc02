//! Bounded input from a caller-owned pipe or console; no reader thread or handle ownership.
use std::io::{self, Write};

#[cfg(unix)]
pub type BorrowedInput<'a> = std::os::fd::BorrowedFd<'a>;
#[cfg(windows)]
pub type BorrowedInput<'a> = std::os::windows::io::BorrowedHandle<'a>;

const INPUT_KIND: &str =
    "interactive input must be a pipe or terminal; regular files are unsupported";

#[cfg(unix)]
pub struct Input<'a> {
    handle: BorrowedInput<'a>,
}
#[cfg(unix)]
impl<'a> Input<'a> {
    pub fn new(handle: BorrowedInput<'a>) -> io::Result<Self> {
        use rustix::fs::{fstat, FileType};
        if FileType::from_raw_mode(fstat(handle)?.st_mode) != FileType::Fifo
            && !rustix::termios::isatty(handle)
        {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, INPUT_KIND));
        }
        Ok(Self { handle })
    }
    pub fn buffered(&self) -> bool {
        false
    }
    /// None means that this pump produced no bytes; Some(0) means EOF.
    pub fn read(
        &mut self,
        bytes: &mut [u8; 4096],
        _output: &mut impl Write,
    ) -> io::Result<Option<usize>> {
        match rustix::io::read(self.handle, bytes) {
            Ok(count) => Ok(Some(count)),
            Err(rustix::io::Errno::INTR) => Ok(None),
            Err(error) => Err(error.into()),
        }
    }
}

#[cfg(windows)]
pub use windows::{ready, Input};

#[cfg(windows)]
mod windows {
    use super::{io, BorrowedInput, Write, INPUT_KIND};
    use std::{collections::VecDeque, os::windows::io::AsRawHandle, ptr};
    use windows_sys::Win32::{
        Foundation::{ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF, ERROR_MORE_DATA, ERROR_NO_DATA},
        Storage::FileSystem::{GetFileType, ReadFile, FILE_TYPE_PIPE},
        System::{
            Console::{
                GetConsoleMode, GetNumberOfConsoleInputEvents, ReadConsoleInputW,
                ENABLE_ECHO_INPUT, INPUT_RECORD, KEY_EVENT,
            },
            Pipes::PeekNamedPipe,
        },
    };

    fn pipe_end(error: &io::Error) -> bool {
        matches!(
            error.raw_os_error().map(|code| code as u32),
            Some(ERROR_BROKEN_PIPE | ERROR_HANDLE_EOF | ERROR_NO_DATA)
        )
    }

    fn pipe_available(handle: BorrowedInput<'_>) -> io::Result<Option<u32>> {
        let mut count = 0;
        // PeekNamedPipe supports both named and anonymous pipes. The caller is
        // the exclusive reader, so a following bounded read cannot lose bytes
        // to another consumer and block waiting for replacement input.
        if unsafe {
            PeekNamedPipe(
                handle.as_raw_handle(),
                ptr::null_mut(),
                0,
                ptr::null_mut(),
                &mut count,
                ptr::null_mut(),
            )
        } != 0
        {
            return Ok(Some(count));
        }
        let error = io::Error::last_os_error();
        if pipe_end(&error) {
            Ok(None)
        } else {
            Err(error)
        }
    }

    fn console_events(handle: BorrowedInput<'_>) -> io::Result<u32> {
        let mut count = 0;
        if unsafe { GetNumberOfConsoleInputEvents(handle.as_raw_handle(), &mut count) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(count)
        }
    }

    pub fn ready(handle: BorrowedInput<'_>) -> io::Result<bool> {
        if unsafe { GetFileType(handle.as_raw_handle()) } == FILE_TYPE_PIPE {
            return Ok(pipe_available(handle)?.is_none_or(|count| count != 0));
        }
        Ok(console_events(handle)? != 0)
    }

    pub struct Input<'a> {
        handle: BorrowedInput<'a>,
        console: Option<Console>,
    }
    impl<'a> Input<'a> {
        pub fn new(handle: BorrowedInput<'a>) -> io::Result<Self> {
            let console = if unsafe { GetFileType(handle.as_raw_handle()) } == FILE_TYPE_PIPE {
                None
            } else {
                let mut mode = 0;
                if unsafe { GetConsoleMode(handle.as_raw_handle(), &mut mode) } == 0 {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, INPUT_KIND));
                }
                // ReadConsoleInputW bypasses the cooked editor. Leave all
                // caller-owned modes unchanged, including processed Ctrl-C.
                Some(Console::new(mode & ENABLE_ECHO_INPUT != 0))
            };
            Ok(Self { handle, console })
        }
        pub fn buffered(&self) -> bool {
            self.console.as_ref().is_some_and(|console| {
                !console.bytes.is_empty() || console.repeat.is_some() || console.eof
            })
        }
        /// None means only console events/no ready pipe bytes, never EOF.
        pub fn read(
            &mut self,
            bytes: &mut [u8; 4096],
            output: &mut impl Write,
        ) -> io::Result<Option<usize>> {
            if let Some(console) = &mut self.console {
                return console.read(self.handle, bytes, output);
            }
            let Some(available) = pipe_available(self.handle)? else {
                return Ok(Some(0));
            };
            if available == 0 {
                return Ok(None);
            }
            let mut count = 0;
            if unsafe {
                ReadFile(
                    self.handle.as_raw_handle(),
                    bytes.as_mut_ptr(),
                    available.min(bytes.len() as u32),
                    &mut count,
                    ptr::null_mut(),
                )
            } != 0
            {
                return Ok(Some(count as usize));
            }
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(ERROR_MORE_DATA as i32) && count != 0 {
                // A message-mode named pipe can return a valid bounded prefix.
                Ok(Some(count as usize))
            } else if pipe_end(&error) {
                Ok(Some(0))
            } else {
                Err(error)
            }
        }
    }

    struct Console {
        line: String,
        bytes: VecDeque<u8>,
        repeat: Option<(u16, u16)>,
        high_surrogate: Option<u16>,
        discarding: bool,
        eof: bool,
        echo: bool,
    }
    impl Console {
        fn new(echo: bool) -> Self {
            Self {
                line: String::new(),
                bytes: VecDeque::new(),
                repeat: None,
                high_surrogate: None,
                discarding: false,
                eof: false,
                echo,
            }
        }
        fn drain(&mut self, bytes: &mut [u8; 4096]) -> Option<usize> {
            let count = self.bytes.len().min(bytes.len());
            for byte in &mut bytes[..count] {
                *byte = self.bytes.pop_front().unwrap();
            }
            (count != 0 || self.eof).then_some(count)
        }
        fn character(&mut self, ch: char, output: &mut impl Write) -> io::Result<()> {
            match ch {
                '\u{8}' => {
                    if self.high_surrogate.take().is_none()
                        && !self.discarding
                        && self.line.pop().is_some()
                        && self.echo
                    {
                        output.write_all(b"\x08 \x08")?;
                    }
                }
                '\r' | '\n' => {
                    self.bytes.extend(self.line.as_bytes());
                    self.line.clear();
                    self.bytes.push_back(b'\n');
                    self.discarding = false;
                    if self.echo {
                        output.write_all(b"\r\n")?;
                    }
                }
                '\u{1a}' => {
                    self.bytes.extend(self.line.as_bytes());
                    self.line.clear();
                    self.eof = true;
                }
                ch if !ch.is_control() || ch == '\t' => {
                    let mut encoded = [0; 4];
                    let encoded = ch.encode_utf8(&mut encoded).as_bytes();
                    if !self.discarding {
                        if self.line.len() + encoded.len() > 4096 {
                            // Deliver the overflow to the existing Lines
                            // boundary, which owns its visible error/recovery.
                            self.bytes.extend(self.line.as_bytes());
                            self.bytes.extend(encoded);
                            self.line.clear();
                            self.discarding = true;
                        } else {
                            self.line.push(ch);
                        }
                    }
                    if self.echo {
                        output.write_all(encoded)?;
                    }
                }
                _ => (),
            }
            Ok(())
        }
        fn unit(&mut self, unit: u16, output: &mut impl Write) -> io::Result<()> {
            if unit == 8 {
                return self.character('\u{8}', output);
            }
            if let Some(high) = self.high_surrogate.take() {
                if (0xdc00..=0xdfff).contains(&unit) {
                    let code =
                        0x10000 + ((u32::from(high) - 0xd800) << 10) + (u32::from(unit) - 0xdc00);
                    return self.character(char::from_u32(code).unwrap(), output);
                }
                self.character(char::REPLACEMENT_CHARACTER, output)?;
            }
            if (0xd800..=0xdbff).contains(&unit) {
                self.high_surrogate = Some(unit);
            } else {
                self.character(
                    char::from_u32(u32::from(unit)).unwrap_or(char::REPLACEMENT_CHARACTER),
                    output,
                )?;
            }
            Ok(())
        }
        fn read(
            &mut self,
            handle: BorrowedInput<'_>,
            bytes: &mut [u8; 4096],
            output: &mut impl Write,
        ) -> io::Result<Option<usize>> {
            if let Some(count) = self.drain(bytes) {
                return Ok(Some(count));
            }
            // Bound both event consumption and key repeats per Lab pump.
            for _ in 0..256 {
                let unit = if let Some((unit, left)) = self.repeat.take() {
                    if left > 1 {
                        self.repeat = Some((unit, left - 1));
                    }
                    unit
                } else {
                    if console_events(handle)? == 0 {
                        break;
                    }
                    let mut event = INPUT_RECORD::default();
                    let mut count = 0;
                    if unsafe {
                        ReadConsoleInputW(handle.as_raw_handle(), &mut event, 1, &mut count)
                    } == 0
                    {
                        return Err(io::Error::last_os_error());
                    }
                    if count == 0 || u32::from(event.EventType) != KEY_EVENT {
                        continue;
                    }
                    let key = unsafe { event.Event.KeyEvent };
                    let unit = unsafe { key.uChar.UnicodeChar };
                    if key.bKeyDown == 0 || unit == 0 {
                        continue;
                    }
                    if key.wRepeatCount > 1 {
                        self.repeat = Some((unit, key.wRepeatCount - 1));
                    }
                    unit
                };
                self.unit(unit, output)?;
                if !self.bytes.is_empty() || self.eof {
                    break;
                }
            }
            output.flush()?;
            Ok(self.drain(bytes))
        }
    }
}
