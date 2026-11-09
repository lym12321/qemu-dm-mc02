//! Bounded borrowed data channels. UART stamps are polling samples; CAN keeps
//! original native wire time. This adapter owns no socket, worker or clock.

use crate::{
    can::{CanBus, CanBusOptions, CanCallbacks, CanDelivery, CanFrame, EndpointId},
    endpoints::qemu_can::{QemuCanEndpoint, QemuTimestampMapper},
    step_wire::{AdcInput, AdcVoltage, V1Frame},
    transports::{fixed84::CanStream, stream::StreamTransport},
    Error, Result,
};
use std::os::fd::{AsFd, BorrowedFd};
use std::{
    collections::BTreeSet,
    io::{Read, Write},
    sync::Arc,
};

const V1_HEADER: usize = 28;
const V1_MAX_BODY: usize = V1_HEADER + 128;

fn admission(message: &'static str) -> crate::Failure {
    Arc::new(Error::Admission(message))
}
fn state(message: &'static str) -> crate::Failure {
    Arc::new(Error::State(message))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChannelData {
    Uart(Vec<u8>),
    Can(CanFrame),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChannelEvent {
    pub endpoint: String,
    pub timestamp_ns: u64,
    pub data: ChannelData,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdcValue {
    Raw(u16),
    Voltage(u32),
}

struct CanPort<'a, T: Read + Write> {
    name: String,
    bus: CanBus,
    qemu: QemuCanEndpoint<'a, CanStream<T>>,
    observer: EndpointId,
}
struct Callbacks<'a, 'io, T: Read + Write> {
    name: &'a str,
    qemu: &'a mut QemuCanEndpoint<'io, CanStream<T>>,
    observer: &'a EndpointId,
    events: &'a mut Vec<ChannelEvent>,
}
impl<T: Read + Write> CanCallbacks for Callbacks<'_, '_, T> {
    fn observe(&mut self, delivery: &CanDelivery<'_>, _: &mut CanBus) -> Result<()> {
        if delivery.source == self.qemu.id() && delivery.destination == self.observer {
            self.events.push(ChannelEvent {
                endpoint: self.name.to_owned(),
                timestamp_ns: delivery.frame.timestamp_ns(),
                data: ChannelData::Can(delivery.frame.clone()),
            });
        }
        Ok(())
    }
    fn receive(
        &mut self,
        destination: &EndpointId,
        frame: &CanFrame,
        _: &mut CanBus,
    ) -> Result<()> {
        if destination == self.qemu.id() {
            self.qemu.receive(frame)?;
        }
        Ok(())
    }
}

/// All streams remain borrowed. Dropping/resetting these logical queues does
/// not flush, close, or discard physical RX/TX suffixes.
pub struct LabChannels<'a, T: Read + Write> {
    uarts: Vec<(String, &'a mut StreamTransport<T>)>,
    cans: Vec<CanPort<'a, T>>,
    cosim: Option<&'a mut StreamTransport<T>>,
    opening_seen: bool,
    time_ns: u64,
    adc_sequence: Option<u64>,
    adc_time_ns: Option<u64>,
}
impl<'a, T: Read + Write> LabChannels<'a, T> {
    pub fn new(
        uarts: Vec<(String, &'a mut StreamTransport<T>)>,
        cans: Vec<(String, &'a mut CanStream<T>)>,
        cosim: Option<&'a mut StreamTransport<T>>,
    ) -> Result<Self> {
        let mut names = BTreeSet::new();
        for name in uarts
            .iter()
            .map(|(name, _)| name)
            .chain(cans.iter().map(|(name, _)| name))
        {
            if name.is_empty() || !names.insert(name) {
                return Err(admission("channel names must be nonempty and unique"));
            }
        }
        let mut ports = Vec::new();
        for (name, stream) in cans {
            let mut bus = CanBus::new(CanBusOptions::default())?;
            let mut qemu = QemuCanEndpoint::new(&mut bus, stream, "qemu")?;
            qemu.bind_mapper(QemuTimestampMapper {
                qemu_origin_ns: 0,
                worker_origin_ns: 0,
            });
            let observer = EndpointId::new();
            bus.attach(&observer, "lab")?;
            ports.push(CanPort {
                name,
                bus,
                qemu,
                observer,
            });
        }
        Ok(Self {
            uarts,
            cans: ports,
            cosim,
            opening_seen: false,
            time_ns: 0,
            adc_sequence: None,
            adc_time_ns: None,
        })
    }
    /// Consume the actual opening telemetry before submitting the initial RESET.
    /// Each call reads at most once; waiting and deadlines belong to the caller.
    pub fn consume_opening(&mut self) -> Result<bool> {
        if self.opening_seen {
            return Ok(true);
        }
        let Some(stream) = &mut self.cosim else {
            return Ok(true);
        };
        stream.read_once()?;
        while let Some(bytes) = stream.next_length_prefixed(V1_HEADER, V1_MAX_BODY)? {
            let frame = V1Frame::decode_body(&bytes)?;
            if frame.kind != 3 || frame.payload.len() != 12 {
                return Err(state("unexpected Lab co-sim opening frame"));
            }
            self.opening_seen = true;
        }
        Ok(self.opening_seen)
    }
    pub fn reset(&mut self, timestamp_ns: u64) -> Result<()> {
        if self.cosim.is_some() && !self.opening_seen {
            return Err(state("consume the Lab co-sim opening marker before RESET"));
        }
        if let Some(stream) = &mut self.cosim {
            send_v1(
                stream,
                &V1Frame {
                    kind: 1,
                    sequence: 0,
                    time_ns: timestamp_ns,
                    payload: Vec::new(),
                },
            )?;
        }
        for port in &mut self.cans {
            port.bus.reset(timestamp_ns)?;
        }
        self.time_ns = timestamp_ns;
        self.adc_sequence = Some(1);
        self.adc_time_ns = Some(timestamp_ns);
        Ok(())
    }
    pub fn send_uart(&mut self, endpoint: &str, data: &[u8]) -> Result<()> {
        let (_, stream) = self
            .uarts
            .iter_mut()
            .find(|(name, _)| name == endpoint)
            .ok_or_else(|| admission("unknown UART endpoint"))?;
        if data.len() > 4096 {
            return Err(admission("UART data exceeds 4096 bytes"));
        }
        if !data.is_empty() {
            stream.send(data.to_vec())?;
        }
        Ok(())
    }
    pub fn send_can(&mut self, endpoint: &str, frame: CanFrame) -> Result<()> {
        if frame.timestamp_ns() < self.time_ns {
            return Err(admission("CAN input predates latest Lab service"));
        }
        let port = self
            .cans
            .iter_mut()
            .find(|port| port.name == endpoint)
            .ok_or_else(|| admission("unknown CAN endpoint"))?;
        port.bus.send(&port.observer, frame, None)
    }
    pub fn send_adc(&mut self, channel: u16, value: AdcValue, timestamp_ns: u64) -> Result<()> {
        let (kind, payload) = match value {
            AdcValue::Raw(raw) => (5, AdcInput { channel, raw }.encode()?),
            AdcValue::Voltage(voltage_uv) => (
                6,
                AdcVoltage {
                    channel,
                    flags: 0,
                    voltage_uv,
                }
                .encode()?,
            ),
        };
        if timestamp_ns < self.time_ns {
            return Err(admission("ADC input predates latest Lab service"));
        }
        let stream = self
            .cosim
            .as_mut()
            .ok_or_else(|| state("ADC co-sim is not configured"))?;
        let previous = self
            .adc_time_ns
            .ok_or_else(|| state("ADC requires an explicit RESET"))?;
        if timestamp_ns <= previous {
            return Err(admission(
                "ADC timestamp must strictly increase after RESET/input",
            ));
        }
        let sequence = self
            .adc_sequence
            .ok_or_else(|| admission("ADC sequence exceeds u64"))?;
        send_v1(
            stream,
            &V1Frame {
                kind,
                sequence,
                time_ns: timestamp_ns,
                payload,
            },
        )?;
        self.adc_sequence = sequence.checked_add(1);
        self.adc_time_ns = Some(timestamp_ns);
        Ok(())
    }
    pub fn service(&mut self, timestamp_ns: u64, flush_uart: bool) -> Result<Vec<ChannelEvent>> {
        if timestamp_ns < self.time_ns {
            return Err(admission("Lab service time cannot decrease"));
        }
        self.time_ns = timestamp_ns;
        let mut events = Vec::new();
        for (name, stream) in &mut self.uarts {
            stream.read_once()?;
            let length = stream.buffered_bytes();
            if length > 0 {
                events.push(ChannelEvent {
                    endpoint: name.clone(),
                    timestamp_ns,
                    data: ChannelData::Uart(
                        stream
                            .next_record(length)?
                            .expect("complete buffered bytes"),
                    ),
                });
            }
            if flush_uart {
                stream.flush()?;
            }
        }
        for port in &mut self.cans {
            for frame in port.qemu.stream_mut().receive()? {
                port.qemu.ingest(&mut port.bus, &frame, timestamp_ns)?;
            }
            port.bus.step(
                timestamp_ns,
                None,
                &mut Callbacks {
                    name: &port.name,
                    qemu: &mut port.qemu,
                    observer: &port.observer,
                    events: &mut events,
                },
            )?;
            port.qemu.stream_mut().transport_mut().flush()?;
        }
        if let Some(stream) = &mut self.cosim {
            stream.read_once()?;
            while let Some(bytes) = stream.next_length_prefixed(V1_HEADER, V1_MAX_BODY)? {
                if V1Frame::decode_body(&bytes)?.kind == 1 {
                    self.adc_time_ns = None;
                }
            }
            stream.flush()?;
        }
        Ok(events)
    }
}
impl<T: Read + Write + AsFd> LabChannels<'_, T> {
    pub(super) fn descriptors(&self) -> Vec<BorrowedFd<'_>> {
        self.uarts
            .iter()
            .map(|(_, stream)| stream.io().as_fd())
            .chain(
                self.cans
                    .iter()
                    .map(|port| port.qemu.stream().transport().io().as_fd()),
            )
            .chain(self.cosim.iter().map(|stream| stream.io().as_fd()))
            .collect()
    }
}
fn send_v1<T: Read + Write>(stream: &mut StreamTransport<T>, frame: &V1Frame) -> Result<()> {
    stream.send_length_prefixed(&frame.encode_body()?, V1_HEADER, V1_MAX_BODY)
}
