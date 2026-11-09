//! Full-speed USB/IP exporter. The guest owns descriptors and every control/
//! endpoint response. The Lab owner pumps this service; it owns no MCU clock,
//! QMP connection, process or detached IO thread.
mod codec;
mod importer;
use crate::transports::stream::StreamTransport;
use codec::*;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    time::{Duration, Instant},
};

const MAX_TRANSFER: usize = 1024 * 1024;
const MAX_BYTES: usize = 8 * MAX_TRANSFER;
const MAX_URBS: usize = 64;
const BUSID: &str = "1-1";
const DEVID: u32 = 0x10001;
fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn word(b: &[u8], at: usize) -> u32 {
    u32::from_be_bytes(b[at..at + 4].try_into().unwrap())
}
fn le16(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes(b[at..at + 2].try_into().unwrap())
}
fn lower(e: crate::Failure) -> io::Error {
    io::Error::other(e)
}

/// Bind before native process creation. USB/IP has no authentication; this
/// product exposes only an explicitly requested loopback listener.
pub fn listen(address: SocketAddr) -> io::Result<TcpListener> {
    if !address.ip().is_loopback() {
        return Err(invalid("USB/IP requires a loopback address"));
    }
    let listener = TcpListener::bind(address)?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}

struct Peer {
    socket: TcpStream,
    rx: Vec<u8>,
    tx: Vec<u8>,
    sent: usize,
    attached: bool,
    closing: bool,
    opened: Instant,
}
impl Peer {
    fn send(&mut self, bytes: Vec<u8>) -> io::Result<()> {
        if self.tx.len() - self.sent + bytes.len() > MAX_BYTES + MAX_URBS * 48 {
            return Err(invalid("USB/IP response queue full"));
        }
        if self.sent != 0 {
            self.tx.drain(..self.sent);
            self.sent = 0;
        }
        self.tx.extend(bytes);
        Ok(())
    }
    fn flush(&mut self) -> io::Result<()> {
        if self.sent < self.tx.len() {
            match self.socket.write(&self.tx[self.sent..]) {
                Ok(0) => return Err(invalid("USB/IP write EOF")),
                Ok(n) => self.sent += n,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => (),
                Err(e) => return Err(e),
            }
        }
        if self.sent == self.tx.len() {
            self.tx.clear();
            self.sent = 0;
        }
        Ok(())
    }
    fn pump(&mut self) -> io::Result<()> {
        self.flush()?;
        if self.closing {
            return Ok(());
        }
        if self.rx.len() > MAX_TRANSFER + 48 {
            return Err(invalid("USB/IP input queue full"));
        }
        let mut bytes = [0; 4096];
        match self.socket.read(&mut bytes) {
            Ok(0) => Err(invalid("USB/IP read EOF")),
            Ok(n) => {
                self.rx.extend_from_slice(&bytes[..n]);
                Ok(())
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => Ok(()),
            Err(e) => Err(e),
        }
    }
}

enum Frame {
    List,
    Import([u8; 32]),
    Submit {
        header: UsbIpHeaderBasic,
        flags: u32,
        length: usize,
        setup: [u8; 8],
        data: Vec<u8>,
    },
    Unlink {
        header: UsbIpHeaderBasic,
        target: u32,
    },
}
/// Admit network lengths before allocating/copying an OUT body. ISO is
/// rejected here, not drained into an unbounded packet-descriptor allocation.
fn decode(b: &[u8], attached: bool, available: usize) -> io::Result<Option<(usize, Frame)>> {
    if !attached {
        if b.len() < 8 {
            return Ok(None);
        }
        if u16::from_be_bytes(b[..2].try_into().unwrap()) != USBIP_VERSION || word(b, 4) != 0 {
            return Err(invalid("invalid USB/IP management header"));
        }
        return match u16::from_be_bytes(b[2..4].try_into().unwrap()) {
            OP_REQ_DEVLIST => Ok(Some((8, Frame::List))),
            OP_REQ_IMPORT if b.len() < 40 => Ok(None),
            OP_REQ_IMPORT => Ok(Some((40, Frame::Import(b[8..40].try_into().unwrap())))),
            _ => Err(invalid("unsupported USB/IP operation")),
        };
    }
    if b.len() < 48 {
        return Ok(None);
    }
    let header = UsbIpHeaderBasic::from_bytes(b[..20].try_into().unwrap());
    if header.direction > 1 || header.ep > 8 || header.devid != DEVID {
        return Err(invalid("invalid USB/IP URB header"));
    }
    match header.command {
        USBIP_CMD_UNLINK => Ok(Some((
            48,
            Frame::Unlink {
                header,
                target: word(b, 20),
            },
        ))),
        USBIP_CMD_SUBMIT => {
            let length = word(b, 24) as usize;
            let packets = word(b, 32);
            if length > MAX_TRANSFER || length > available || (packets != 0 && packets != u32::MAX)
            {
                return Err(invalid("USB/IP transfer exceeds limits or requests ISO"));
            }
            let total = 48 + if header.direction == 0 { length } else { 0 };
            let setup: [u8; 8] = b[40..48].try_into().unwrap();
            if header.ep == 0
                && (length != le16(&setup, 6) as usize
                    || (length != 0 && header.direction != (setup[0] >> 7) as u32))
            {
                return Err(invalid("USB/IP control direction/length mismatch"));
            }
            if b.len() < total {
                return Ok(None);
            }
            let flags = word(b, 20);
            Ok(Some((
                total,
                Frame::Submit {
                    header,
                    flags,
                    length,
                    setup,
                    data: b[48..total].to_vec(),
                },
            )))
        }
        _ => Err(invalid("unsupported USB/IP URB command")),
    }
}

#[derive(Clone)]
enum Enumeration {
    Reset,
    Device,
    Address(Vec<u8>),
    ConfigHeader(Vec<u8>),
    Config(Vec<u8>),
}
enum Operation {
    Enumeration(Enumeration),
    Urb {
        peer: u64,
        header: UsbIpHeaderBasic,
        length: usize,
        flags: u32,
    },
}
struct Unlink {
    peer: u64,
    header: UsbIpHeaderBasic,
}

pub struct Exporter<'a, T: Read + Write> {
    native: &'a mut StreamTransport<T>,
    listener: TcpListener,
    peers: BTreeMap<u64, Peer>,
    next_peer: u64,
    next_id: u32,
    pending: BTreeMap<u32, Operation>,
    unlinks: BTreeMap<u32, Unlink>,
    body_length: Option<usize>,
    imported: Option<u64>,
    connected: bool,
    device: Option<UsbDevice>,
    enumeration_deadline: Option<Instant>,
    reset_urb: Option<(u64, UsbIpHeaderBasic)>,
    message: String,
    importer: importer::Importer,
}
impl<'a, T: Read + Write> Exporter<'a, T> {
    pub fn new(native: &'a mut StreamTransport<T>, listener: TcpListener) -> Self {
        Self {
            native,
            listener,
            peers: BTreeMap::new(),
            next_peer: 0,
            next_id: 0,
            pending: BTreeMap::new(),
            unlinks: BTreeMap::new(),
            body_length: None,
            imported: None,
            connected: false,
            device: None,
            enumeration_deadline: None,
            reset_urb: None,
            message: "waiting for firmware USB".into(),
            importer: importer::Importer::default(),
        }
    }
    pub fn attach(&mut self) -> io::Result<()> {
        if self.device.is_none() || self.imported.is_some() {
            return Err(invalid("USB firmware device is not available"));
        }
        self.importer.attach(self.listener.local_addr()?, BUSID)
    }
    pub fn detach(&mut self) -> io::Result<()> {
        self.importer.detach()
    }
    pub fn close(&mut self) -> io::Result<()> {
        self.importer.close()
    }
    pub fn status(&self) -> Value {
        json!({"address":self.listener.local_addr().ok().map(|a|a.to_string()),"bus_id":BUSID,
            "state":if self.imported.is_some(){"attached"}else if self.device.is_some(){"available"}else if self.enumeration_deadline.is_some(){"enumerating"}else{"disconnected"},
            "message":self.importer.message().unwrap_or(&self.message),"local_port":self.importer.port(),"importer_busy":self.importer.busy(),"pending_urbs":self.pending.len(),
            "vid":self.device.as_ref().map(|d|d.vendor_id),"pid":self.device.as_ref().map(|d|d.product_id)})
    }
    fn allocated(&self) -> usize {
        self.pending
            .values()
            .map(|op| match op {
                Operation::Urb { length, .. } => *length,
                _ => 0,
            })
            .sum()
    }
    fn id(&mut self) -> u32 {
        loop {
            self.next_id = self.next_id.wrapping_add(1);
            if self.next_id != 0
                && !self.pending.contains_key(&self.next_id)
                && !self.unlinks.contains_key(&self.next_id)
            {
                return self.next_id;
            }
        }
    }
    // The arguments follow the fixed native v1 request header and payload.
    #[allow(clippy::too_many_arguments)]
    fn request(
        &mut self,
        kind: u32,
        id: u32,
        ep: u32,
        in_: bool,
        length: usize,
        flags: u32,
        setup: [u8; 8],
        data: &[u8],
    ) -> io::Result<()> {
        let mut b = Vec::with_capacity(36 + data.len());
        for v in [
            32 + data.len() as u32,
            kind,
            id,
            ep,
            in_ as u32,
            length as u32,
            flags,
        ] {
            b.extend_from_slice(&v.to_be_bytes());
        }
        b.extend_from_slice(&setup);
        b.extend_from_slice(data);
        self.native.send(b).map_err(lower)
    }
    fn enumerate(&mut self, step: Enumeration) -> io::Result<()> {
        let (kind, setup) = match step {
            Enumeration::Reset => (3, [0; 8]),
            Enumeration::Device => (1, [0x80, 6, 0, 1, 0, 0, 18, 0]),
            Enumeration::Address(_) => (1, [0, 5, 1, 0, 0, 0, 0, 0]),
            Enumeration::ConfigHeader(_) => (1, [0x80, 6, 0, 2, 0, 0, 9, 0]),
            Enumeration::Config(_) => return Err(invalid("missing configuration length")),
        };
        let id = self.id();
        self.request(
            kind,
            id,
            0,
            setup[0] & 128 != 0,
            le16(&setup, 6) as usize,
            0,
            setup,
            &[],
        )?;
        self.pending.insert(id, Operation::Enumeration(step));
        Ok(())
    }
    fn begin_enumeration(&mut self) -> io::Result<()> {
        self.pending
            .retain(|_, op| !matches!(op, Operation::Enumeration(_)));
        self.device = None;
        self.enumeration_deadline = Some(Instant::now() + Duration::from_secs(10));
        self.message = "enumerating guest firmware".into();
        self.enumerate(Enumeration::Reset)
    }
    fn enumeration_reply(
        &mut self,
        step: Enumeration,
        status: i32,
        data: Vec<u8>,
    ) -> io::Result<()> {
        if status != 0 {
            return self.enumeration_failed(format!("guest enumeration failed: {status}"));
        }
        match step {
            Enumeration::Reset => self.enumerate(Enumeration::Device),
            Enumeration::Device => {
                if data.len() != 18
                    || data[0] != 18
                    || data[1] != 1
                    || !matches!(data[7], 8 | 16 | 32 | 64)
                {
                    return Err(invalid("invalid guest device descriptor"));
                }
                self.enumerate(Enumeration::Address(data))
            }
            Enumeration::Address(device) => self.enumerate(Enumeration::ConfigHeader(device)),
            Enumeration::ConfigHeader(device) => {
                if data.len() != 9 || data[1] != 2 || le16(&data, 2) < 9 || le16(&data, 2) > 4096 {
                    return Err(invalid("invalid guest configuration header"));
                }
                let n = le16(&data, 2);
                let id = self.id();
                self.request(
                    1,
                    id,
                    0,
                    true,
                    n as usize,
                    0,
                    [0x80, 6, 0, 2, 0, 0, n as u8, (n >> 8) as u8],
                    &[],
                )?;
                self.pending
                    .insert(id, Operation::Enumeration(Enumeration::Config(device)));
                Ok(())
            }
            Enumeration::Config(device) => {
                let metadata = metadata(&device, &data)?;
                self.message = format!(
                    "{:04x}:{:04x} ready",
                    metadata.vendor_id, metadata.product_id
                );
                self.device = Some(metadata);
                self.enumeration_deadline = None;
                if let Some((peer, header)) = self.reset_urb.take() {
                    self.send_submit(peer, header, 0, 0, Vec::new())?;
                }
                Ok(())
            }
        }
    }
    fn enumeration_failed(&mut self, message: String) -> io::Result<()> {
        let ids = self
            .pending
            .iter()
            .filter_map(|(&id, op)| matches!(op, Operation::Enumeration(_)).then_some(id))
            .collect::<Vec<_>>();
        for id in ids {
            self.pending.remove(&id);
            self.request(2, id, 0, false, 0, 0, [0; 8], &[])?;
        }
        self.enumeration_deadline = None;
        self.message = message;
        if let Some((peer, header)) = self.reset_urb.take() {
            self.send_submit(peer, header, -19, 0, Vec::new())?;
        }
        Ok(())
    }
    fn send_peer(&mut self, peer: u64, bytes: Vec<u8>) -> io::Result<()> {
        if let Some(p) = self.peers.get_mut(&peer) {
            if p.send(bytes).is_err() {
                self.drop_peer(peer)?;
            }
        }
        Ok(())
    }
    fn submit_bytes(
        mut header: UsbIpHeaderBasic,
        status: i32,
        actual: u32,
        data: Vec<u8>,
    ) -> Vec<u8> {
        header.command = USBIP_RET_SUBMIT;
        // Replies retain original direction for payload serialization; Linux
        // and usbip-win2 accept the original devid/endpoint identifiers.
        UsbIpResponse::UsbIpRetSubmit {
            header,
            status: status as u32,
            actual_length: actual,
            start_frame: 0,
            number_of_packets: 0,
            error_count: 0,
            transfer_buffer: data,
            iso_packet_descriptor: vec![],
        }
        .to_bytes()
    }
    fn send_submit(
        &mut self,
        peer: u64,
        header: UsbIpHeaderBasic,
        status: i32,
        actual: u32,
        data: Vec<u8>,
    ) -> io::Result<()> {
        self.send_peer(peer, Self::submit_bytes(header, status, actual, data))
    }
    fn send_unlink(&mut self, unlink: Unlink, status: i32) -> io::Result<()> {
        let mut header = unlink.header;
        header.command = USBIP_RET_UNLINK;
        self.send_peer(
            unlink.peer,
            UsbIpResponse::UsbIpRetUnlink {
                header,
                status: status as u32,
            }
            .to_bytes(),
        )
    }
    fn native_reply(&mut self, b: Vec<u8>) -> io::Result<()> {
        let kind = word(&b, 0);
        let id = word(&b, 4);
        let status = word(&b, 8) as i32;
        let actual = word(&b, 12);
        if kind == 4 {
            if id != 1 || !matches!(status, 0 | 1) || b.len() != 16 {
                return Err(invalid("invalid USB bridge opening/event"));
            }
            let connected = status == 1;
            if connected && !self.connected {
                self.connected = true;
                self.begin_enumeration()?;
            }
            if !connected {
                self.connected = false;
                self.device = None;
                self.enumeration_deadline = None;
                self.message = "firmware USB disconnected".into();
            }
            return Ok(());
        }
        if kind == 2 {
            if status == -104 {
                self.pending.remove(&id);
            }
            if let Some(unlink) = self.unlinks.remove(&id) {
                self.send_unlink(unlink, if status == -104 { -104 } else { 0 })?;
            }
            return Ok(());
        }
        if !matches!(kind, 1 | 3) {
            return Err(invalid("invalid USB bridge reply kind"));
        }
        let Some(op) = self.pending.remove(&id) else {
            return Ok(());
        };
        match op {
            Operation::Enumeration(step) => self.enumeration_reply(step, status, b[16..].to_vec()),
            Operation::Urb {
                peer,
                header,
                length,
                flags,
            } => {
                let in_ = header.direction == 1;
                if actual as usize > length || b.len() != 16 + if in_ { actual as usize } else { 0 }
                {
                    return Err(invalid("invalid USB bridge completion length"));
                }
                let status = if status == 0 && in_ && flags & 1 != 0 && (actual as usize) < length {
                    -121
                } else {
                    status
                };
                self.send_submit(peer, header, status, actual, b[16..].to_vec())
            }
        }
    }
    fn drop_peer(&mut self, peer: u64) -> io::Result<()> {
        self.peers.remove(&peer);
        let ids: Vec<_> = self
            .pending
            .iter()
            .filter_map(|(&id, op)| {
                matches!(op,Operation::Urb{peer:p,..} if *p==peer).then_some(id)
            })
            .collect();
        for id in ids {
            self.request(2, id, 0, false, 0, 0, [0; 8], &[])?;
        }
        if self.imported == Some(peer) {
            self.imported = None;
            if self.connected {
                self.begin_enumeration()?;
            }
        }
        Ok(())
    }
    fn handle(&mut self, id: u64, peer: &mut Peer, frame: Frame) -> io::Result<()> {
        match frame {
            Frame::List => {
                let devices = if self.imported.is_none() {
                    self.device.iter().cloned().collect::<Vec<_>>()
                } else {
                    vec![]
                };
                peer.send(
                    UsbIpResponse::OpRepDevlist {
                        status: 0,
                        device_count: devices.len() as u32,
                        devices,
                    }
                    .to_bytes(),
                )?;
                peer.closing = true;
            }
            Frame::Import(busid) => {
                let mut expected = [0; 32];
                expected[..BUSID.len()].copy_from_slice(BUSID.as_bytes());
                let device = if busid == expected && self.imported.is_none() {
                    self.device.clone()
                } else {
                    None
                };
                let success = device.is_some();
                peer.send(
                    UsbIpResponse::OpRepImport {
                        status: if success { 0 } else { 1 },
                        device,
                    }
                    .to_bytes(),
                )?;
                if success {
                    peer.attached = true;
                    self.imported = Some(id);
                } else {
                    peer.closing = true;
                }
            }
            Frame::Submit {
                header,
                flags,
                length,
                setup,
                data,
            } => {
                if self.pending.values().any(|op|matches!(op,Operation::Urb{peer:p,header:h,..} if *p==id && h.seqnum==header.seqnum)) || self.unlinks.values().any(|u|u.peer==id && u.header.seqnum==header.seqnum){return Err(invalid("duplicate USB/IP sequence"));}
                if self.pending.len() >= MAX_URBS || self.allocated() + length > MAX_BYTES {
                    return Err(invalid("USB/IP pending capacity full"));
                }
                if header.ep == 0
                    && setup[0] == 0x23
                    && setup[1] == 3
                    && le16(&setup, 2) == 4
                    && length == 0
                {
                    if self.reset_urb.is_some() {
                        return Err(invalid("USB reset already pending"));
                    }
                    self.reset_urb = Some((id, header));
                    self.begin_enumeration()?;
                } else {
                    let native_id = self.id();
                    self.request(
                        1,
                        native_id,
                        header.ep,
                        header.direction == 1,
                        length,
                        flags,
                        setup,
                        &data,
                    )?;
                    self.pending.insert(
                        native_id,
                        Operation::Urb {
                            peer: id,
                            header,
                            length,
                            flags,
                        },
                    );
                }
            }
            Frame::Unlink { mut header, target } => {
                let found=self.pending.iter().find_map(|(&native,op)|matches!(op,Operation::Urb{peer:p,header:h,..} if *p==id && h.seqnum==target).then_some(native));
                if let Some(native) = found.filter(|n| !self.unlinks.contains_key(n)) {
                    self.request(2, native, 0, false, 0, 0, [0; 8], &[])?;
                    self.unlinks.insert(native, Unlink { peer: id, header });
                } else {
                    header.command = USBIP_RET_UNLINK;
                    peer.send(UsbIpResponse::UsbIpRetUnlink { header, status: 0 }.to_bytes())?;
                }
            }
        }
        Ok(())
    }
    pub fn service(&mut self) -> io::Result<()> {
        self.importer.poll()?;
        self.native.flush().map_err(lower)?;
        self.native.read_once().map_err(lower)?;
        // At most one 4096-byte native read per owner iteration; drain only
        // complete bounded records that this read made available.
        loop {
            if self.body_length.is_none() {
                let Some(b) = self.native.next_record(4).map_err(lower)? else {
                    break;
                };
                let n = word(&b, 0) as usize;
                if !(16..=MAX_TRANSFER + 16).contains(&n) {
                    return Err(invalid("invalid USB bridge frame length"));
                }
                self.body_length = Some(n);
            }
            let Some(b) = self
                .native
                .next_record(self.body_length.unwrap())
                .map_err(lower)?
            else {
                break;
            };
            self.body_length = None;
            self.native_reply(b)?;
        }
        if self
            .enumeration_deadline
            .is_some_and(|d| Instant::now() >= d)
        {
            self.enumeration_failed("guest USB enumeration timed out".into())?;
        }
        match self.listener.accept() {
            Ok((socket, _)) if self.peers.len() < 4 => {
                socket.set_nonblocking(true)?;
                socket.set_nodelay(true)?;
                self.next_peer += 1;
                self.peers.insert(
                    self.next_peer,
                    Peer {
                        socket,
                        rx: vec![],
                        tx: vec![],
                        sent: 0,
                        attached: false,
                        closing: false,
                        opened: Instant::now(),
                    },
                );
            }
            Ok((_socket, _)) => (),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => (),
            Err(e) => return Err(e),
        }
        let ids = self.peers.keys().copied().collect::<Vec<_>>();
        for id in ids {
            let Some(mut peer) = self.peers.remove(&id) else {
                continue;
            };
            let result = (|| -> io::Result<()> {
                peer.pump()?;
                if !peer.attached && peer.opened.elapsed() > Duration::from_secs(3) {
                    return Err(invalid("USB/IP handshake expired"));
                }
                if !peer.closing {
                    if let Some((used, frame)) =
                        decode(&peer.rx, peer.attached, MAX_BYTES - self.allocated())?
                    {
                        peer.rx.drain(..used);
                        self.handle(id, &mut peer, frame)?;
                    }
                }
                peer.flush()?;
                Ok(())
            })();
            if result.is_err() || (peer.closing && peer.tx.is_empty()) {
                // A malformed/closed importer affects only its own session.
                if let Err(e) = result {
                    self.message = format!("USB/IP peer closed: {e}");
                }
                self.drop_peer(id)?;
            } else {
                self.peers.insert(id, peer);
            }
        }
        Ok(())
    }
}

fn metadata(device: &[u8], config: &[u8]) -> io::Result<UsbDevice> {
    if config.len() < 9
        || config[0] != 9
        || config[1] != 2
        || le16(config, 2) as usize != config.len()
    {
        return Err(invalid("invalid guest configuration descriptor"));
    }
    let mut interfaces = BTreeMap::new();
    let mut at = 0;
    while at < config.len() {
        let n = config[at] as usize;
        if n < 2 || at + n > config.len() {
            return Err(invalid("invalid guest descriptor boundary"));
        }
        if config[at + 1] == 4 {
            if n < 9 {
                return Err(invalid("short guest interface descriptor"));
            }
            if config[at + 3] == 0 {
                interfaces.insert(
                    config[at + 2],
                    UsbInterface {
                        interface_class: config[at + 5],
                        interface_subclass: config[at + 6],
                        interface_protocol: config[at + 7],
                    },
                );
            }
        }
        if config[at + 1] == 5 && (n < 7 || config[at + 3] & 3 == 1 || le16(config, at + 4) > 64) {
            return Err(invalid(
                "USB/IP supports full-speed control/bulk/interrupt only",
            ));
        }
        at += n;
    }
    if interfaces.len() != config[4] as usize {
        return Err(invalid("guest interface count mismatch"));
    }
    Ok(UsbDevice {
        path: "/sim/usb/1-1".into(),
        bus_id: BUSID.into(),
        bus_num: 1,
        dev_num: 1,
        speed: 2,
        vendor_id: le16(device, 8),
        product_id: le16(device, 10),
        device_bcd: le16(device, 12),
        device_class: device[4],
        device_subclass: device[5],
        device_protocol: device[6],
        configuration_value: config[5],
        num_configurations: device[17],
        interfaces: interfaces.into_values().collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_untrusted_lengths_before_body() {
        let mut b = [0; 48];
        b[..4].copy_from_slice(&1u32.to_be_bytes());
        b[8..12].copy_from_slice(&DEVID.to_be_bytes());
        b[12..16].copy_from_slice(&1u32.to_be_bytes());
        b[16..20].copy_from_slice(&1u32.to_be_bytes());
        for n in [u32::MAX, MAX_TRANSFER as u32 + 1] {
            b[24..28].copy_from_slice(&n.to_be_bytes());
            assert!(decode(&b, true, MAX_BYTES).is_err());
        }
        b[24..28].copy_from_slice(&64u32.to_be_bytes());
        b[32..36].copy_from_slice(&1u32.to_be_bytes());
        assert!(decode(&b, true, MAX_BYTES).is_err());
        b[32..36].fill(0);
        assert!(decode(&b, true, 63).is_err());
        b[12..16].copy_from_slice(&2u32.to_be_bytes());
        assert!(decode(&b, true, MAX_BYTES).is_err());
    }
    #[test]
    fn metadata_preserves_wire_bcd_and_interfaces() {
        let d = metadata(
            &[
                18, 1, 0x10, 2, 0xef, 2, 1, 64, 0x83, 4, 0x41, 0x57, 0x23, 1, 1, 2, 3, 1,
            ],
            &[9, 2, 18, 0, 1, 1, 0, 128, 50, 9, 4, 0, 0, 0, 0xff, 0, 0, 0],
        )
        .unwrap();
        let b = d.to_bytes_with_interfaces();
        assert_eq!(
            &b[300..312],
            &[4, 0x83, 0x57, 0x41, 1, 0x23, 0xef, 2, 1, 1, 1, 1]
        );
        assert_eq!(&b[312..], &[0xff, 0, 0, 0]);
    }
}
