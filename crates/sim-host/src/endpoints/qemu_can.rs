//! Borrowed QEMU CAN endpoint. RESET origins are supplied by the caller;
//! mapping never advances the bus, native machine or external plant.

use crate::{
    can::{CanBus, CanFrame, EndpointId},
    transports::fixed84::CanFrameSink,
    Error, Result,
};
use std::sync::Arc;

#[derive(Clone, Copy, Debug)]
pub struct QemuTimestampMapper {
    pub qemu_origin_ns: u64,
    pub worker_origin_ns: u64,
}

impl QemuTimestampMapper {
    pub fn map(&self, timestamp_ns: u64, fallback_ns: u64) -> Result<u64> {
        match timestamp_ns.checked_sub(self.qemu_origin_ns) {
            None => Ok(fallback_ns),
            Some(delta) => self
                .worker_origin_ns
                .checked_add(delta)
                .ok_or_else(|| Arc::new(Error::Admission("mapped CAN time exceeds u64"))),
        }
    }
    pub fn unmap(&self, timestamp_ns: u64, fallback_ns: u64) -> Result<u64> {
        match timestamp_ns.checked_sub(self.worker_origin_ns) {
            None => Ok(fallback_ns),
            Some(delta) => self
                .qemu_origin_ns
                .checked_add(delta)
                .ok_or_else(|| Arc::new(Error::Admission("QEMU CAN time exceeds u64"))),
        }
    }
}

pub struct QemuCanEndpoint<'a, T: CanFrameSink + ?Sized> {
    bus_identity: Arc<()>,
    id: EndpointId,
    stream: &'a mut T,
    mapper: Option<QemuTimestampMapper>,
    submitted_frames: u64,
}

impl<'a, T: CanFrameSink + ?Sized> QemuCanEndpoint<'a, T> {
    pub fn new(bus: &mut CanBus, stream: &'a mut T, name: impl Into<String>) -> Result<Self> {
        let id = EndpointId::new();
        bus.attach(&id, name)?;
        Ok(Self {
            bus_identity: bus.identity().clone(),
            id,
            stream,
            mapper: None,
            submitted_frames: 0,
        })
    }
    pub fn id(&self) -> &EndpointId {
        &self.id
    }
    pub fn stream(&self) -> &T {
        self.stream
    }
    pub fn stream_mut(&mut self) -> &mut T {
        self.stream
    }
    pub fn submitted_frames(&self) -> u64 {
        self.submitted_frames
    }
    pub fn bind_mapper(&mut self, mapper: QemuTimestampMapper) {
        self.mapper = Some(mapper);
    }

    fn mapper(&self) -> Result<QemuTimestampMapper> {
        self.mapper
            .ok_or_else(|| Arc::new(Error::State("QEMU CAN endpoint requires a RESET mapper")))
    }

    pub fn ingest(&self, bus: &mut CanBus, frame: &CanFrame, current_time_ns: u64) -> Result<()> {
        if !Arc::ptr_eq(&self.bus_identity, bus.identity()) {
            return Err(Arc::new(Error::Admission(
                "QEMU CAN endpoint belongs to another bus",
            )));
        }
        let timestamp = self.mapper()?.map(frame.timestamp_ns(), current_time_ns)?;
        bus.send(&self.id, frame.with_timestamp(timestamp), None)
    }

    /// Count only successful send returns, including records still queued.
    /// An error may follow physical IO; the caller must freeze rather than replay.
    pub fn receive(&mut self, frame: &CanFrame) -> Result<()> {
        let mapper = self.mapper()?;
        let timestamp = mapper.unmap(frame.timestamp_ns(), mapper.qemu_origin_ns)?;
        let count = self
            .submitted_frames
            .checked_add(1)
            .ok_or_else(|| Arc::new(Error::Admission("CAN submission count exceeds u64")))?;
        self.stream.send_frame(&frame.with_timestamp(timestamp))?;
        self.submitted_frames = count;
        Ok(())
    }
}
