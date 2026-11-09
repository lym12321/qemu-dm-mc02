//! Deterministic host CAN broadcasts, without electrical arbitration.
//!
//! The bus owns its bounded pending queue, attachment order and first failure.
//! Endpoints and callbacks remain owned by its consumer. Metadata never
//! advances a CPU or establishes a CPU/plant time barrier.

use crate::{Error, Failure, Result};
use std::{cmp::Ordering, collections::BinaryHeap, sync::Arc};

pub const EXTENDED: u32 = 1;
pub const RTR: u32 = 2;
pub const FD: u32 = 4;
pub const BRS: u32 = 8;
pub const FLAGS: u32 = EXTENDED | RTR | FD | BRS;
const FD_LENGTHS: [usize; 16] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64];

pub fn dlc_to_length(dlc: u8, fd: bool) -> Result<usize> {
    if dlc > 15 || (!fd && dlc > 8) {
        return Err(Arc::new(Error::Admission("invalid CAN DLC")));
    }
    Ok(FD_LENGTHS[usize::from(dlc)])
}

pub fn length_to_dlc(length: usize, fd: bool) -> Result<u8> {
    FD_LENGTHS
        .iter()
        .position(|&size| size == length && (fd || size <= 8))
        .map(|index| index as u8)
        .ok_or_else(|| Arc::new(Error::Admission("CAN payload length is not representable")))
}

/// Validated immutable fields. A shorter payload may occupy an explicit DLC;
/// the transport supplies zero padding, as on the existing fixed84 boundary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CanFrame {
    can_id: u32,
    flags: u32,
    dlc: u8,
    data: Vec<u8>,
    timestamp_ns: u64,
}

impl CanFrame {
    pub fn new(
        can_id: u32,
        flags: u32,
        dlc: Option<u8>,
        data: impl Into<Vec<u8>>,
        timestamp_ns: u64,
    ) -> Result<Self> {
        if flags & !FLAGS != 0
            || can_id
                > if flags & EXTENDED != 0 {
                    0x1fff_ffff
                } else {
                    0x7ff
                }
            || flags & (FD | RTR) == FD | RTR
            || flags & BRS != 0 && flags & FD == 0
        {
            return Err(Arc::new(Error::Admission(
                "invalid CAN identifier or flags",
            )));
        }
        let data = data.into();
        let fd = flags & FD != 0;
        let dlc = match dlc {
            Some(dlc) => dlc,
            None => length_to_dlc(data.len(), fd)?,
        };
        if data.len() > dlc_to_length(dlc, fd)? {
            return Err(Arc::new(Error::Admission("CAN payload exceeds its DLC")));
        }
        Ok(Self {
            can_id,
            flags,
            dlc,
            data,
            timestamp_ns,
        })
    }

    pub fn can_id(&self) -> u32 {
        self.can_id
    }
    pub fn flags(&self) -> u32 {
        self.flags
    }
    pub fn dlc(&self) -> u8 {
        self.dlc
    }
    pub fn data(&self) -> &[u8] {
        &self.data
    }
    pub fn timestamp_ns(&self) -> u64 {
        self.timestamp_ns
    }
    pub fn length(&self) -> usize {
        FD_LENGTHS[usize::from(self.dlc)]
    }

    pub fn with_timestamp(&self, timestamp_ns: u64) -> Self {
        Self {
            timestamp_ns,
            ..self.clone()
        }
    }
}

/// Opaque attachment identity; equal names or slots on another bus do not match.
#[derive(Clone, Debug)]
pub struct EndpointId(Arc<()>);

impl EndpointId {
    pub fn new() -> Self {
        Self(Arc::new(()))
    }
}

impl Default for EndpointId {
    fn default() -> Self {
        Self::new()
    }
}

impl PartialEq for EndpointId {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}
impl Eq for EndpointId {}

/// Per-destination evidence retains the original source frame. The receive
/// callback separately gets a frame stamped with the actual delivery cut.
#[derive(Debug)]
pub struct CanDelivery<'a> {
    pub source: &'a EndpointId,
    pub destination: &'a EndpointId,
    pub source_name: &'a str,
    pub destination_name: &'a str,
    pub frame: &'a CanFrame,
    pub sequence: u64,
    pub due_ns: u64,
    pub delivered_ns: u64,
    pub late: bool,
    pub dropped: bool,
}

/// Routing belongs to the actual consumer, rather than an owned device inside
/// the bus. Each hook may send into this same queue. Lifecycle changes and
/// recursive stepping are rejected during delivery.
pub trait CanCallbacks {
    fn should_drop(&mut self, _: &CanDelivery<'_>, _: &mut CanBus) -> Result<bool> {
        Ok(false)
    }
    fn observe(&mut self, _: &CanDelivery<'_>, _: &mut CanBus) -> Result<()> {
        Ok(())
    }
    fn receive(
        &mut self,
        destination: &EndpointId,
        frame: &CanFrame,
        bus: &mut CanBus,
    ) -> Result<()>;
}

#[derive(Clone, Debug)]
struct Attached {
    endpoint: EndpointId,
    name: String,
}

#[derive(Debug)]
struct Pending {
    due_ns: u64,
    sequence: u64,
    source: Attached,
    frame: CanFrame,
}

impl PartialEq for Pending {
    fn eq(&self, other: &Self) -> bool {
        (self.due_ns, self.sequence) == (other.due_ns, other.sequence)
    }
}
impl Eq for Pending {}
impl PartialOrd for Pending {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Pending {
    fn cmp(&self, other: &Self) -> Ordering {
        (other.due_ns, other.sequence).cmp(&(self.due_ns, self.sequence))
    }
}

#[derive(Clone, Debug)]
pub struct CanBusOptions {
    pub delay_ns: u64,
    pub loopback: bool,
    pub max_pending: usize,
    pub dispatch_budget: usize,
}

impl Default for CanBusOptions {
    fn default() -> Self {
        Self {
            delay_ns: 0,
            loopback: false,
            max_pending: 256,
            dispatch_budget: 256,
        }
    }
}

#[derive(Debug)]
pub struct CanBus {
    identity: Arc<()>,
    options: CanBusOptions,
    endpoints: Vec<Attached>,
    queue: BinaryHeap<Pending>,
    sequence: u64,
    time_ns: u64,
    started: bool,
    dispatching: bool,
    closed: bool,
    failure: Option<Failure>,
}

impl CanBus {
    pub fn new(options: CanBusOptions) -> Result<Self> {
        if options.max_pending == 0 || options.dispatch_budget == 0 {
            return Err(Arc::new(Error::Admission(
                "CAN capacities must be positive",
            )));
        }
        Ok(Self {
            identity: Arc::new(()),
            options,
            endpoints: Vec::new(),
            queue: BinaryHeap::new(),
            sequence: 0,
            time_ns: 0,
            started: false,
            dispatching: false,
            closed: false,
            failure: None,
        })
    }

    fn healthy(&self) -> Result<()> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.closed {
            return Err(Arc::new(Error::State("CAN bus is closed")));
        }
        Ok(())
    }

    fn idle(&self) -> Result<()> {
        if self.closed {
            return Err(Arc::new(Error::State("CAN bus is closed")));
        }
        if self.dispatching {
            return Err(Arc::new(Error::State(
                "CAN lifecycle/policy cannot change during delivery",
            )));
        }
        Ok(())
    }

    fn freeze(&mut self, error: Failure) -> Failure {
        self.failure.get_or_insert(error).clone()
    }

    pub fn failure(&self) -> Option<&Failure> {
        self.failure.as_ref()
    }
    pub(crate) fn identity(&self) -> &Arc<()> {
        &self.identity
    }
    pub fn pending(&self) -> usize {
        self.queue.len()
    }
    pub fn time_ns(&self) -> u64 {
        self.time_ns
    }
    pub fn delay_ns(&self) -> u64 {
        self.options.delay_ns
    }
    pub fn dispatch_budget(&self) -> usize {
        self.options.dispatch_budget
    }

    pub fn next_due_ns(&self) -> Result<Option<u64>> {
        if !self.closed {
            self.healthy()?;
        }
        Ok(self.queue.peek().map(|item| item.due_ns))
    }

    pub fn has_due(&self, time_ns: u64) -> Result<bool> {
        self.healthy()?;
        Ok(self.queue.peek().is_some_and(|item| item.due_ns <= time_ns))
    }

    pub fn set_delay_ns(&mut self, delay_ns: u64) -> Result<()> {
        self.healthy()?;
        self.idle()?;
        self.options.delay_ns = delay_ns;
        Ok(())
    }

    pub fn attach(&mut self, endpoint: &EndpointId, name: impl Into<String>) -> Result<()> {
        self.idle()?;
        if self.started {
            return Err(Arc::new(Error::State("CAN wiring is already fixed")));
        }
        let name = name.into();
        if name.is_empty()
            || self
                .endpoints
                .iter()
                .any(|item| item.endpoint == *endpoint || item.name == name)
        {
            return Err(Arc::new(Error::Admission(
                "duplicate or empty CAN attachment",
            )));
        }
        self.endpoints.push(Attached {
            endpoint: endpoint.clone(),
            name,
        });
        Ok(())
    }

    pub fn send(
        &mut self,
        source: &EndpointId,
        frame: CanFrame,
        not_before_ns: Option<u64>,
    ) -> Result<()> {
        self.healthy()?;
        let source = self
            .endpoints
            .iter()
            .find(|item| item.endpoint == *source)
            .ok_or_else(|| Arc::new(Error::Admission("CAN source is not attached")))?;
        let due_ns = frame
            .timestamp_ns
            .max(not_before_ns.unwrap_or(0))
            .checked_add(self.options.delay_ns)
            .ok_or_else(|| Arc::new(Error::Admission("CAN delivery time exceeds u64")))?;
        let next_sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| Arc::new(Error::Admission("CAN sequence exceeds u64")))?;
        if self.queue.len() >= self.options.max_pending {
            return Err(self.freeze(Arc::new(Error::Capacity("CAN pending capacity exhausted"))));
        }
        self.queue.push(Pending {
            due_ns,
            sequence: self.sequence,
            source: source.clone(),
            frame,
        });
        self.sequence = next_sequence;
        self.started = true;
        Ok(())
    }

    pub fn step(
        &mut self,
        time_ns: u64,
        max_dispatch: Option<usize>,
        callbacks: &mut impl CanCallbacks,
    ) -> Result<usize> {
        self.healthy()?;
        self.idle()?;
        if time_ns < self.time_ns {
            return Err(Arc::new(Error::Admission("CAN time cannot decrease")));
        }
        let budget = self
            .options
            .dispatch_budget
            .min(max_dispatch.unwrap_or(usize::MAX));
        self.time_ns = time_ns;
        self.started = true;
        self.dispatching = true;
        let result = self.dispatch(time_ns, budget, callbacks);
        self.dispatching = false;
        result.map_err(|error| self.freeze(error))
    }

    fn dispatch(
        &mut self,
        time_ns: u64,
        budget: usize,
        callbacks: &mut impl CanCallbacks,
    ) -> Result<usize> {
        let mut count = 0;
        while count < budget && self.queue.peek().is_some_and(|item| item.due_ns <= time_ns) {
            let pending = self.queue.pop().expect("due queue head exists");
            count += 1;
            let frame = pending.frame.with_timestamp(time_ns);
            for index in 0..self.endpoints.len() {
                let destination = self.endpoints[index].clone();
                if !self.options.loopback && destination.endpoint == pending.source.endpoint {
                    continue;
                }
                let mut delivery = CanDelivery {
                    source: &pending.source.endpoint,
                    destination: &destination.endpoint,
                    source_name: &pending.source.name,
                    destination_name: &destination.name,
                    frame: &pending.frame,
                    sequence: pending.sequence,
                    due_ns: pending.due_ns,
                    delivered_ns: time_ns,
                    late: pending.due_ns < time_ns,
                    dropped: false,
                };
                delivery.dropped = callbacks.should_drop(&delivery, self)?;
                self.healthy()?;
                callbacks.observe(&delivery, self)?;
                self.healthy()?;
                if !delivery.dropped {
                    callbacks.receive(&destination.endpoint, &frame, self)?;
                    self.healthy()?;
                }
            }
        }
        Ok(count)
    }

    /// Logical reset preserves attachment order, wiring lock and delay policy.
    pub fn reset(&mut self, time_ns: u64) -> Result<()> {
        self.idle()?;
        self.queue.clear();
        self.sequence = 0;
        self.time_ns = time_ns;
        self.failure = None;
        Ok(())
    }

    /// Clear owned frames only; callbacks, endpoint devices and IO stay borrowed.
    pub fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        self.idle()?;
        self.closed = true;
        self.queue.clear();
        self.endpoints.clear();
        Ok(())
    }
}
