//! Strict-before matching over completed observations, shared by live runs
//! and replay. Callers own endpoint admission, timestamp mapping and IO.

use crate::{
    can::{CanFrame, EXTENDED, FD},
    Error, Result,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CanMatch {
    pub can_id: u32,
    pub flags: Option<u32>,
    pub dlc: Option<u8>,
    pub data: Option<Vec<u8>>,
}

impl CanMatch {
    pub fn validate(&self) -> Result<()> {
        // Omitted fields are unconstrained. Admit one possible canonical
        // frame without imposing classic/standard flags on the actual match.
        let flags = self.flags.unwrap_or_else(|| {
            (u32::from(self.can_id > 0x7ff) * EXTENDED)
                | (u32::from(
                    self.dlc.is_some_and(|v| v > 8)
                        || self.data.as_ref().is_some_and(|v| v.len() > 8),
                ) * FD)
        });
        CanFrame::new(
            self.can_id,
            flags,
            self.dlc,
            self.data.clone().unwrap_or_default(),
            0,
        )?;
        Ok(())
    }

    fn matches(&self, frame: &CanFrame) -> bool {
        self.can_id == frame.can_id()
            && self.flags.is_none_or(|v| v == frame.flags())
            && self.dlc.is_none_or(|v| v == frame.dlc())
            && self
                .data
                .as_ref()
                .is_none_or(|v| v.as_slice() == frame.data())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Condition {
    UartContains(Vec<u8>),
    DigitalEquals(bool),
    CanSeen(CanMatch),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Assertion {
    pub before_ns: u64,
    pub endpoint: String,
    pub condition: Condition,
}

impl Assertion {
    pub fn validate(&self) -> Result<()> {
        if self.before_ns == 0 || self.endpoint.is_empty() {
            return Err(
                Error::Admission("assertion requires endpoint and positive deadline").into(),
            );
        }
        match &self.condition {
            Condition::UartContains(bytes) if bytes.is_empty() || bytes.len() > 4096 => {
                Err(Error::Admission("UART pattern requires 1..4096 bytes").into())
            }
            Condition::CanSeen(value) => value.validate(),
            _ => Ok(()),
        }
    }

    pub fn kind(&self) -> &'static str {
        match self.condition {
            Condition::UartContains(_) => "uart_contains",
            Condition::DigitalEquals(_) => "digital_equals",
            Condition::CanSeen(_) => "can_seen",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Pending,
    Pass,
    Fail,
}

pub enum ObservationData<'a> {
    Uart(&'a [u8]),
    Can(&'a CanFrame),
}

pub struct Observation<'a> {
    pub endpoint: &'a str,
    pub timestamp_ns: u64,
    pub data: ObservationData<'a>,
}

pub struct Evaluator {
    assertions: Vec<Assertion>,
    status: Vec<Status>,
    tails: Vec<Vec<u8>>,
    origin_ns: u64,
    end_ns: u64,
    time_ns: u64,
    finished: bool,
}

impl Evaluator {
    pub fn new(assertions: Vec<Assertion>, duration_ns: u64, origin_ns: u64) -> Result<Self> {
        for assertion in &assertions {
            assertion.validate()?;
        }
        let end_ns = origin_ns.checked_add(duration_ns).ok_or_else(|| {
            crate::Failure::from(Error::Admission("assertion duration exceeds u64"))
        })?;
        Ok(Self {
            status: vec![Status::Pending; assertions.len()],
            tails: vec![Vec::new(); assertions.len()],
            assertions,
            origin_ns,
            end_ns,
            time_ns: origin_ns,
            finished: false,
        })
    }

    pub fn status(&self) -> &[Status] {
        &self.status
    }
    pub fn all_passed(&self) -> bool {
        self.status.iter().all(|v| *v == Status::Pass)
    }
    pub fn origin_ns(&self) -> u64 {
        self.origin_ns
    }

    pub fn failures(&self) -> Vec<String> {
        self.assertions
            .iter()
            .zip(&self.status)
            .enumerate()
            .filter(|(_, (_, status))| **status == Status::Fail)
            .map(|(index, (assertion, _))| {
                format!(
                    "assertion[{}] {} {} did not match before {} ns",
                    index + 1,
                    assertion.kind(),
                    assertion.endpoint,
                    assertion.before_ns
                )
            })
            .collect()
    }

    fn check_clock(&self, timestamp_ns: u64) -> Result<()> {
        if timestamp_ns < self.time_ns {
            return Err(Error::Admission("assertion observation time cannot decrease").into());
        }
        Ok(())
    }

    fn expire(&mut self, bound_ns: u64) {
        for ((assertion, status), tail) in self
            .assertions
            .iter()
            .zip(&mut self.status)
            .zip(&mut self.tails)
        {
            if *status == Status::Pending && bound_ns - self.origin_ns >= assertion.before_ns {
                *status = Status::Fail;
                tail.clear();
            }
        }
    }

    /// Validate the entire batch before matching. Expiry may be deferred for
    /// multiple batches at one completed cut; advance() closes that cut.
    /// UART timestamps must be actual completed upper bounds supplied by the
    /// consumer; CAN keeps its admitted mapped wire time.
    pub fn observe(
        &mut self,
        digital: &[(&str, bool)],
        events: &[Observation<'_>],
        bound_ns: u64,
        expire: bool,
    ) -> Result<()> {
        self.check_clock(bound_ns)?;
        if self.finished {
            return Err(Error::State("experiment is finished").into());
        }
        if digital.iter().any(|(endpoint, _)| endpoint.is_empty())
            || events
                .iter()
                .any(|event| event.endpoint.is_empty() || event.timestamp_ns > bound_ns)
        {
            return Err(Error::Admission("invalid observation endpoint or completed bound").into());
        }
        for ((assertion, status), tail) in self
            .assertions
            .iter()
            .zip(&mut self.status)
            .zip(&mut self.tails)
        {
            if *status != Status::Pending {
                continue;
            }
            let matched = match &assertion.condition {
                Condition::DigitalEquals(wanted) => {
                    bound_ns - self.origin_ns < assertion.before_ns
                        && digital.iter().any(|(endpoint, level)| {
                            *endpoint == assertion.endpoint && level == wanted
                        })
                }
                condition => events.iter().any(|event| {
                    if event.endpoint != assertion.endpoint
                        || event
                            .timestamp_ns
                            .checked_sub(self.origin_ns)
                            .is_none_or(|elapsed| elapsed >= assertion.before_ns)
                    {
                        return false;
                    }
                    match (condition, &event.data) {
                        (Condition::UartContains(pattern), ObservationData::Uart(bytes)) => {
                            match_uart(tail, pattern, bytes)
                        }
                        (Condition::CanSeen(wanted), ObservationData::Can(frame)) => {
                            wanted.matches(frame)
                        }
                        _ => false,
                    }
                }),
            };
            if matched {
                *status = Status::Pass;
                tail.clear();
            }
        }
        if expire {
            self.expire(bound_ns);
        }
        self.time_ns = bound_ns;
        Ok(())
    }

    pub fn advance(&mut self, timestamp_ns: u64) -> Result<()> {
        self.check_clock(timestamp_ns)?;
        if self.finished {
            return Err(Error::State("experiment is finished").into());
        }
        self.expire(timestamp_ns);
        self.time_ns = timestamp_ns;
        Ok(())
    }

    /// Repeated finish at the same or a later bound is harmless. Failed
    /// assertions stay terminal even if a later observation has older metadata.
    pub fn finish(&mut self, timestamp_ns: u64) -> Result<&[Status]> {
        self.check_clock(timestamp_ns)?;
        if timestamp_ns < self.end_ns {
            return Err(Error::Admission("experiment cannot finish before its duration").into());
        }
        self.expire(timestamp_ns);
        self.time_ns = timestamp_ns;
        self.finished = true;
        Ok(&self.status)
    }
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|value| value == needle)
}

fn match_uart(tail: &mut Vec<u8>, pattern: &[u8], bytes: &[u8]) -> bool {
    let keep = pattern.len() - 1;
    // Only the crossing prefix needs a temporary buffer. Neither this buffer
    // nor retained state grows with the observation transcript.
    let mut crossing = tail.clone();
    crossing.extend_from_slice(&bytes[..bytes.len().min(keep)]);
    if contains(&crossing, pattern) || contains(bytes, pattern) {
        return true;
    }
    if bytes.len() >= keep {
        tail.clear();
        tail.extend_from_slice(&bytes[bytes.len() - keep..]);
    } else {
        tail.extend_from_slice(bytes);
        if tail.len() > keep {
            tail.drain(..tail.len() - keep);
        }
    }
    false
}
