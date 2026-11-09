//! Linux SocketCAN acquisition and bounded atomic datagrams using socketcan 4.0.
//! Native record time is assigned by the caller's completed Simulation cut.

use crate::{
    can::{length_to_dlc, CanFrame, BRS, EXTENDED, FD, RTR},
    transports::fixed84::CanFrameSink,
    Error, Failure, Result,
};
use socketcan::{
    frame::AsPtr,
    id::{
        FdFlags, CANFD_BRS, CANFD_ESI, CANFD_FDF, CAN_EFF_FLAG, CAN_EFF_MASK, CAN_ERR_FLAG,
        CAN_RTR_FLAG, CAN_SFF_MASK,
    },
    CanAnyFrame, CanDataFrame, CanFdFrame, CanFdSocket, CanRawFrame, CanRemoteFrame, CanSocket,
    EmbeddedFrame, ExtendedId, Id, Socket, StandardId,
};
use std::{collections::VecDeque, io, sync::Arc};

fn invalid(message: &'static str) -> Failure {
    Arc::new(Error::Admission(message))
}
/// Mature frame constructors own Linux layout and padding; the host only maps flags.
pub fn encode(frame: &CanFrame) -> Result<CanAnyFrame> {
    let id = if frame.flags() & EXTENDED != 0 {
        Id::Extended(ExtendedId::new(frame.can_id()).expect("validated extended ID"))
    } else {
        Id::Standard(StandardId::new(frame.can_id() as u16).expect("validated standard ID"))
    };
    let mut padded = [0; 64];
    padded[..frame.data().len()].copy_from_slice(frame.data());
    if frame.flags() & FD != 0 {
        let flags = if frame.flags() & BRS != 0 {
            FdFlags::BRS
        } else {
            FdFlags::empty()
        };
        Ok(CanFdFrame::with_flags(id, &padded[..frame.length()], flags)
            .expect("validated FD length")
            .into())
    } else if frame.flags() & RTR != 0 {
        let native = CanRemoteFrame::new_remote(id, frame.length()).expect("validated remote DLC");
        // The existing DM wire retains RTR payload bytes as opaque transport data.
        let mut raw = *native.as_ref();
        raw.data[..frame.data().len()].copy_from_slice(frame.data());
        Ok(CanRemoteFrame::try_from(raw)
            .expect("validated remote record")
            .into())
    } else {
        Ok(CanDataFrame::new(id, &padded[..frame.length()])
            .expect("validated classic length")
            .into())
    }
}
/// Validate raw fields before socketcan's normalizing conversions can hide bad input.
pub fn decode(raw: CanRawFrame, timestamp_ns: u64) -> Result<CanFrame> {
    let (id, length, fd_flags, data, reserved) = match &raw {
        CanRawFrame::Classic(frame) => {
            // libc represents every byte of these fixed records, including reserved
            // fields. socketcan initializes them before recv, without uninitialized gaps.
            let bytes = unsafe {
                std::slice::from_raw_parts(
                    (frame as *const _) as *const u8,
                    std::mem::size_of_val(frame),
                )
            };
            (
                frame.can_id,
                usize::from(frame.can_dlc),
                None,
                &frame.data[..],
                bytes[5..8].iter().any(|b| *b != 0),
            )
        }
        CanRawFrame::Fd(frame) => {
            let bytes = unsafe {
                std::slice::from_raw_parts(
                    (frame as *const _) as *const u8,
                    std::mem::size_of_val(frame),
                )
            };
            (
                frame.can_id,
                usize::from(frame.len),
                Some(frame.flags),
                &frame.data[..],
                bytes[6..8].iter().any(|b| *b != 0),
            )
        }
    };
    if reserved {
        return Err(invalid("SocketCAN record contains nonzero reserved bytes"));
    }
    if id & CAN_ERR_FLAG != 0 {
        return Err(invalid(
            "SocketCAN error frames are not representable on DM wire",
        ));
    }
    let extended = id & CAN_EFF_FLAG != 0;
    if !extended && id & !(CAN_SFF_MASK | CAN_RTR_FLAG) != 0 {
        return Err(invalid("SocketCAN standard ID contains unsupported bits"));
    }
    let mut flags =
        if extended { EXTENDED } else { 0 } | if id & CAN_RTR_FLAG != 0 { RTR } else { 0 };
    if let Some(native_flags) = fd_flags {
        let native_flags = i32::from(native_flags);
        if native_flags & !(CANFD_BRS | CANFD_ESI | CANFD_FDF) != 0
            || native_flags & CANFD_FDF == 0
            || native_flags & CANFD_ESI != 0
            || id & CAN_RTR_FLAG != 0
        {
            return Err(invalid(
                "SocketCAN FD flags are not representable on DM wire",
            ));
        }
        flags |= FD
            | if native_flags & CANFD_BRS != 0 {
                BRS
            } else {
                0
            };
    }
    let dlc = length_to_dlc(length, fd_flags.is_some())?;
    CanFrame::new(
        id & if extended { CAN_EFF_MASK } else { CAN_SFF_MASK },
        flags,
        Some(dlc),
        data[..length].to_vec(),
        timestamp_ns,
    )
}
/// One datagram per call. Implementations never retry a partial datagram.
pub trait CanDatagramIo {
    fn receive(&mut self) -> io::Result<CanRawFrame>;
    fn send(&mut self, frame: &CanAnyFrame) -> io::Result<usize>;
}
pub enum NativeCanSocket {
    Classic(CanSocket),
    Fd(CanFdSocket),
}
impl NativeCanSocket {
    pub fn open(interface: &str, enable_fd: bool) -> Result<Self> {
        validate_interface(interface)?;
        let socket = if enable_fd {
            Self::Fd(CanFdSocket::open(interface).map_err(|e| Arc::new(Error::Io(e)))?)
        } else {
            Self::Classic(CanSocket::open(interface).map_err(|e| Arc::new(Error::Io(e)))?)
        };
        // RAII keeps partially configured acquisition owned until success.
        match &socket {
            Self::Classic(s) => s.set_nonblocking(true),
            Self::Fd(s) => s.set_nonblocking(true),
        }
        .map_err(|e| Arc::new(Error::Io(e)))?;
        Ok(socket)
    }
}
impl CanDatagramIo for NativeCanSocket {
    fn receive(&mut self) -> io::Result<CanRawFrame> {
        match self {
            Self::Classic(s) => s.read_raw_frame().map(Into::into),
            Self::Fd(s) => s.read_raw_frame(),
        }
    }
    fn send(&mut self, frame: &CanAnyFrame) -> io::Result<usize> {
        // All byte values are initialized by socketcan's frame constructors.
        let bytes = unsafe { frame.as_bytes() };
        // write_frame uses write_all; a datagram must use one send instead.
        match self {
            Self::Classic(s) => s.as_raw_socket().send(bytes),
            Self::Fd(s) => s.as_raw_socket().send(bytes),
        }
    }
}
impl<T: CanDatagramIo + ?Sized> CanDatagramIo for Box<T> {
    fn receive(&mut self) -> io::Result<CanRawFrame> {
        (**self).receive()
    }
    fn send(&mut self, frame: &CanAnyFrame) -> io::Result<usize> {
        (**self).send(frame)
    }
}
pub fn validate_interface(interface: &str) -> Result<()> {
    if interface.is_empty()
        || interface.len() > 15
        || !interface
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.:-".contains(&c))
    {
        return Err(invalid(
            "SocketCAN interface requires 1..15 ASCII name bytes",
        ));
    }
    Ok(())
}
/// Own complete pending datagrams; logical session changes do not discard them.
pub struct SocketCanTransport<T: CanDatagramIo> {
    io: T,
    queue: VecDeque<CanAnyFrame>,
    max_pending: usize,
    io_budget: usize,
    enable_fd: bool,
    failure: Option<Failure>,
}
impl<T: CanDatagramIo> SocketCanTransport<T> {
    pub fn new(io: T, enable_fd: bool, max_pending: usize, io_budget: usize) -> Result<Self> {
        if !(1..=65536).contains(&max_pending) || !(1..=4096).contains(&io_budget) {
            return Err(invalid(
                "SocketCAN requires max_pending1..65536 and io_budget1..4096",
            ));
        }
        Ok(Self {
            io,
            queue: VecDeque::new(),
            max_pending,
            io_budget,
            enable_fd,
            failure: None,
        })
    }
    fn check(&self) -> Result<()> {
        if let Some(error) = &self.failure {
            Err(error.clone())
        } else {
            Ok(())
        }
    }
    fn freeze(&mut self, error: Failure) -> Failure {
        self.failure.get_or_insert(error).clone()
    }
    pub fn failure(&self) -> Option<&Failure> {
        self.failure.as_ref()
    }
    pub fn pending(&self) -> usize {
        self.queue.len()
    }
    pub fn io(&self) -> &T {
        &self.io
    }
    pub fn io_mut(&mut self) -> &mut T {
        &mut self.io
    }
    pub fn flush(&mut self) -> Result<bool> {
        self.check()?;
        for _ in 0..self.io_budget {
            let Some(frame) = self.queue.front() else {
                return Ok(true);
            };
            match self.io.send(frame) {
                Ok(n) if n == frame.size() => {
                    self.queue.pop_front();
                }
                Ok(_) => {
                    return Err(self.freeze(Arc::new(Error::Io(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "short SocketCAN datagram write",
                    )))))
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    return Ok(false)
                }
                Err(error) => return Err(self.freeze(Arc::new(Error::Io(error)))),
            }
        }
        Ok(self.queue.is_empty())
    }
    pub fn receive_once(&mut self, cut: u64) -> Result<Option<CanFrame>> {
        self.check()?;
        match self.io.receive() {
            Ok(raw) => decode(raw, cut)
                .map(Some)
                .map_err(|error| self.freeze(error)),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                Ok(None)
            }
            Err(error) => Err(self.freeze(Arc::new(Error::Io(error)))),
        }
    }
}
impl<T: CanDatagramIo> CanFrameSink for SocketCanTransport<T> {
    fn send_frame(&mut self, frame: &CanFrame) -> Result<()> {
        self.check()?;
        if frame.flags() & FD != 0 && !self.enable_fd {
            return Err(invalid("SocketCAN FD is disabled"));
        }
        if self.queue.len() >= self.max_pending {
            self.flush()?;
        }
        if self.queue.len() >= self.max_pending {
            return Err(Arc::new(Error::Capacity("SocketCAN TX queue is full")));
        }
        self.queue.push_back(encode(frame)?);
        self.flush()?;
        Ok(())
    }
}
