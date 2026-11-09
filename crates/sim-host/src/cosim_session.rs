//! One host v2 session over a borrowed canonical stream. This is a narrow port
//! of QemuCosimEndpoint/StepSessionValidator's strict response path. The native
//! link remains the sole source-ID owner; ACK never attests BMI088 consumption.
use crate::{step_wire as wire, transports::stream::StreamTransport};
use sim_core::{Failure, Result};
use std::{
    collections::{BTreeMap, VecDeque},
    io::{Read, Write},
    sync::Arc,
};

pub const CAP_STEP: u32 = 1;
pub const CAP_IMU: u32 = 2;
pub const CAP_ADC: u32 = 8;
const MAX_OUTSTANDING: usize = 64;
const SERVICE_FRAMES: usize = 64;
const MIN_BODY: usize = 28;
const MAX_BODY: usize = wire::BODY_HEADER_SIZE + wire::MAX_PAYLOAD;
fn admission(message: impl Into<String>) -> Failure {
    Arc::new(sim_core::Error::Admission(message.into()))
}
fn state_error(message: impl Into<String>) -> Failure {
    Arc::new(sim_core::Error::State(message.into()))
}
fn backend_error(message: impl Into<String>) -> Failure {
    Arc::new(sim_core::Error::BackendUnavailable(message.into()))
}
fn lower(error: crate::Failure) -> Failure {
    error
}

#[derive(Clone, Copy, Debug)]
pub struct SessionOptions {
    pub session_id: u32,
    pub sim_origin_ns: u64,
    pub native_origin_ns: u64,
    pub required_capabilities: u32,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Response {
    Reset,
    ResetAck(wire::ResetAck),
    StepAck(wire::StepAck),
    StepDone(wire::StepDone),
    Telemetry(wire::Telemetry),
    Diagnostics(wire::Diagnostics),
    MotorState(wire::MotorState),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Observation {
    pub frame: wire::Frame,
    pub response: Response,
}
#[derive(Clone, Copy)]
struct Pending {
    time: u64,
    dt: u64,
    has_imu: bool,
    acked: bool,
}

pub struct CosimSession {
    options: SessionOptions,
    worker: Option<bool>,
    reset_baseline: u64,
    last_wire: Option<Vec<u8>>,
    optional_done: VecDeque<((u64, u64, u64), bool)>,
    last_done: Option<(u64, u64, u64)>,
    replayed_done: Option<(u64, u64, u64)>,
    reset_sent: bool,
    reset_ack: Option<wire::ResetAck>,
    last_submit: (u64, u64),
    last_ack: (u64, u64, u64),
    pending: BTreeMap<u64, Pending>,
    telemetry: Option<(u64, u64)>,
    failure: Option<Failure>,
    closed: bool,
}
impl CosimSession {
    pub fn new(options: SessionOptions) -> Result<Self> {
        if options.session_id == 0
            || options.native_origin_ns > i64::MAX as u64
            || options.required_capabilities & !63 != 0
            || options.required_capabilities & CAP_STEP == 0
        {
            return Err(admission("co-sim requires nonzero session ID, signed64 native origin and known STEP capabilities"));
        }
        Ok(Self {
            options,
            worker: None,
            reset_baseline: 0,
            last_wire: None,
            optional_done: VecDeque::new(),
            last_done: None,
            replayed_done: None,
            reset_sent: false,
            reset_ack: None,
            last_submit: (0, options.sim_origin_ns),
            last_ack: (0, options.sim_origin_ns, 0),
            pending: BTreeMap::new(),
            telemetry: None,
            failure: None,
            closed: false,
        })
    }
    /// Worker compatibility permits bounded STEP replay and optional DONE, while
    /// strict native-machine consumers retain their existing admission policy.
    pub fn new_worker(options: SessionOptions, require_done: bool) -> Result<Self> {
        let mut session = Self::new(options)?;
        session.worker = Some(require_done);
        Ok(session)
    }
    pub fn native_origin_ns(&self) -> u64 {
        self.options.native_origin_ns
    }
    pub fn reset_origin_ns(&self) -> u64 {
        self.options.sim_origin_ns
    }
    pub fn requires_done(&self) -> bool {
        self.worker != Some(false)
    }
    /// Retransmit exact admitted bytes only after its physical suffix drained.
    pub fn retry_last<T: Read + Write>(&mut self, stream: &mut StreamTransport<T>) -> Result<bool> {
        self.check()?;
        if self.worker.is_none() {
            return Err(state_error("STEP retry belongs to worker mode"));
        }
        let bytes = self
            .last_wire
            .as_ref()
            .ok_or_else(|| state_error("no admitted worker STEP to replay"))?;
        if stream.tx_pending() {
            return Ok(false);
        }
        let result = stream
            .send_length_prefixed(bytes, wire::BODY_HEADER_SIZE, MAX_BODY)
            .map_err(lower);
        self.freeze(result)?;
        Ok(true)
    }
    pub fn generate_session_id() -> Result<u32> {
        let mut bytes = [0; 4];
        std::fs::File::open("/dev/urandom")
            .and_then(|mut file| file.read_exact(&mut bytes))
            .map_err(|error| Arc::new(error) as Failure)?;
        Ok(u32::from_ne_bytes(bytes).max(1))
    }
    pub fn session_id(&self) -> u32 {
        self.options.session_id
    }
    pub fn started(&self) -> bool {
        self.reset_ack.is_some()
    }
    pub fn outstanding(&self) -> usize {
        self.pending.len()
    }
    pub fn last_submission(&self) -> (u64, u64) {
        self.last_submit
    }
    pub fn acknowledged(&self, step_id: u64) -> bool {
        step_id > 0 && step_id <= self.last_ack.0
    }
    pub fn failure(&self) -> Option<&Failure> {
        self.failure.as_ref()
    }
    fn check(&self) -> Result<()> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.closed {
            return Err(state_error("co-sim session is closed"));
        }
        Ok(())
    }
    fn freeze<T>(&mut self, result: Result<T>) -> Result<T> {
        result.map_err(|error| self.failure.get_or_insert(error).clone())
    }
    fn frame(
        &self,
        kind: wire::Kind,
        step_id: u64,
        time_ns: u64,
        dt_ns: u64,
        payload: Vec<u8>,
    ) -> wire::Frame {
        wire::Frame {
            kind,
            step_id,
            time_ns,
            dt_ns,
            session_id: self.options.session_id,
            payload,
        }
    }
    pub fn begin_reset<T: Read + Write>(&mut self, stream: &mut StreamTransport<T>) -> Result<()> {
        self.check()?;
        if self.reset_sent {
            return Err(state_error("co-sim RESET may be sent only once"));
        }
        let bytes = self
            .frame(wire::Kind::Reset, 0, self.options.sim_origin_ns, 0, vec![])
            .encode_body()
            .map_err(lower)?;
        self.reset_sent = true;
        let result = stream
            .send_length_prefixed(&bytes, wire::BODY_HEADER_SIZE, MAX_BODY)
            .map_err(lower);
        self.freeze(result)
    }
    /// Reserve before the first possible write. ADC-only inputs retain no DONE
    /// obligation; IMU records remain until the corresponding ordered DONE.
    pub fn submit<T: Read + Write>(
        &mut self,
        stream: &mut StreamTransport<T>,
        time: u64,
        input: wire::StepInput,
    ) -> Result<u64> {
        self.check()?;
        if !self.started() {
            return Err(state_error("co-sim STEP requires RESET_ACK"));
        }
        let dt = time
            .checked_sub(self.last_submit.1)
            .filter(|dt| *dt > 0)
            .ok_or_else(|| admission("co-sim input time must increase strictly"))?;
        time.checked_sub(self.options.sim_origin_ns)
            .and_then(|delta| self.options.native_origin_ns.checked_add(delta))
            .filter(|native| *native <= i64::MAX as u64)
            .ok_or_else(|| admission("mapped native co-sim time exceeds signed64"))?;
        let step_id = self
            .last_submit
            .0
            .checked_add(1)
            .ok_or_else(|| state_error("co-sim step ID exhausted"))?;
        let has_imu = match &input {
            wire::StepInput::CompactImu(_) => true,
            wire::StepInput::Sections(sections) => sections
                .iter()
                .any(|section| matches!(section, wire::InputSection::Imu(_))),
        };
        let payload = input.encode().map_err(lower)?;
        let bytes = self
            .frame(wire::Kind::Step, step_id, time, dt, payload)
            .encode_body()
            .map_err(lower)?;
        if self.pending.len() >= MAX_OUTSTANDING {
            return Err(Arc::new(sim_core::Error::Capacity(
                "co-sim outstanding input capacity is 64".into(),
            )));
        }
        self.pending.insert(
            step_id,
            Pending {
                time,
                dt,
                has_imu,
                acked: false,
            },
        );
        self.last_submit = (step_id, time);
        if self.worker.is_some() {
            self.last_wire = Some(bytes.clone());
        }
        let result = stream
            .send_length_prefixed(&bytes, wire::BODY_HEADER_SIZE, MAX_BODY)
            .map_err(lower);
        self.freeze(result)?;
        Ok(step_id)
    }
    fn accept(&mut self, frame: wire::Frame) -> Result<Observation> {
        if frame.kind == wire::Kind::Reset {
            if self.worker.is_none() {
                return Err(state_error("unexpected QEMU co-sim session RESET"));
            }
            if !frame.payload.is_empty() || frame.dt_ns != 0 {
                return Err(state_error(
                    "worker RESET requires empty payload and zero dt",
                ));
            }
            self.options.session_id = frame.session_id;
            self.options.sim_origin_ns = frame.time_ns;
            self.options.native_origin_ns = frame.time_ns;
            self.reset_baseline = frame.step_id;
            self.last_submit = (frame.step_id, frame.time_ns);
            self.last_ack = (frame.step_id, frame.time_ns, 0);
            self.reset_ack = None;
            self.reset_sent = true;
            self.pending.clear();
            self.last_wire = None;
            self.optional_done.clear();
            self.last_done = None;
            self.replayed_done = None;
            self.telemetry = None;
            return Ok(Observation {
                frame,
                response: Response::Reset,
            });
        }
        if frame.session_id != self.options.session_id {
            return Err(state_error("co-sim response session ID mismatch"));
        }
        let record = (frame.step_id, frame.time_ns, frame.dt_ns);
        let response = if frame.kind == wire::Kind::ResetAck {
            let ack = wire::ResetAck::decode(&frame.payload).map_err(lower)?;
            if !self.reset_sent
                || self.reset_ack.is_some()
                || record != (self.reset_baseline, self.options.sim_origin_ns, 0)
                || ack.status != 0
                || self.worker.is_none() && ack.virtual_time_ns != self.options.native_origin_ns
            {
                return Err(state_error(
                    "co-sim RESET_ACK does not match native origin and reset baseline",
                ));
            }
            if ack.capabilities & self.options.required_capabilities
                != self.options.required_capabilities
            {
                return Err(backend_error(
                    "co-sim RESET_ACK lacks enabled STEP/IMU/ADC capabilities",
                ));
            }
            if self.worker.is_some() {
                self.options.native_origin_ns = ack.virtual_time_ns;
            }
            self.reset_ack = Some(ack);
            Response::ResetAck(ack)
        } else {
            if !self.started() {
                return Err(state_error("co-sim response requires RESET_ACK first"));
            }
            match frame.kind {
                wire::Kind::StepAck => {
                    let ack = wire::StepAck::decode(&frame.payload).map_err(lower)?;
                    if self.worker.is_some() && record == self.last_ack && ack.status == 0 {
                        self.replayed_done = Some(record);
                        return Ok(Observation {
                            frame,
                            response: Response::StepAck(ack),
                        });
                    }
                    let pending = self
                        .pending
                        .get_mut(&frame.step_id)
                        .ok_or_else(|| state_error("co-sim STEP_ACK has no matching input"))?;
                    if pending.acked
                        || (pending.time, pending.dt) != (frame.time_ns, frame.dt_ns)
                        || frame.step_id <= self.last_ack.0
                        || frame.time_ns <= self.last_ack.1
                        || frame.time_ns - self.last_ack.1 != frame.dt_ns
                    {
                        return Err(state_error(
                            "co-sim STEP_ACK duplicate, out of order or mismatched input",
                        ));
                    }
                    if self.worker.is_some() && ack.status == 1 {
                        return Ok(Observation {
                            frame,
                            response: Response::StepAck(ack),
                        });
                    }
                    if ack.status != 0 {
                        return Err(state_error(format!(
                            "co-sim STEP_ACK failed: status={}",
                            ack.status
                        )));
                    }
                    pending.acked = true;
                    self.last_ack = record;
                    if !pending.has_imu || self.worker == Some(false) {
                        if pending.has_imu {
                            self.optional_done.push_back((record, false));
                            if self.optional_done.len() > 1024 {
                                self.optional_done.pop_front();
                            }
                        }
                        self.pending.remove(&frame.step_id);
                    }
                    Response::StepAck(ack)
                }
                wire::Kind::StepDone => {
                    let done = wire::StepDone::decode(&frame.payload).map_err(lower)?;
                    if self.worker == Some(false) {
                        let previous = self
                            .optional_done
                            .iter_mut()
                            .find(|(known, _)| *known == record)
                            .ok_or_else(|| {
                                state_error("advisory STEP_DONE has no matching STEP_ACK")
                            })?;
                        if previous.1
                            && !(self.replayed_done == Some(record)
                                && self.last_done == Some(record))
                        {
                            return Err(state_error("duplicate advisory STEP_DONE"));
                        }
                        previous.1 = true;
                        if record == self.last_ack {
                            self.last_done = Some(record);
                        }
                        self.replayed_done = None;
                        return Ok(Observation {
                            frame,
                            response: Response::StepDone(done),
                        });
                    }
                    if self.worker.is_some()
                        && self.pending.is_empty()
                        && self.replayed_done == Some(record)
                        && self.last_done == Some(record)
                    {
                        self.replayed_done = None;
                        return Ok(Observation {
                            frame,
                            response: Response::StepDone(done),
                        });
                    }
                    let expected = self
                        .pending
                        .iter()
                        .find(|(_, pending)| pending.has_imu && pending.acked);
                    if expected
                        .is_none_or(|(id, pending)| (*id, pending.time, pending.dt) != record)
                    {
                        return Err(state_error(
                            "co-sim STEP_DONE has no ordered outstanding IMU acceptance",
                        ));
                    }
                    self.pending.remove(&frame.step_id);
                    if self.worker.is_some() {
                        self.last_done = Some(record);
                        self.replayed_done = None;
                    }
                    Response::StepDone(done)
                }
                wire::Kind::Telemetry => {
                    let telemetry = wire::Telemetry::decode(&frame.payload).map_err(lower)?;
                    if self.telemetry.is_some_and(|previous| {
                        frame.step_id <= previous.0 || frame.time_ns <= previous.1
                    }) {
                        return Err(state_error("co-sim telemetry response is out of order"));
                    }
                    self.telemetry = Some((frame.step_id, frame.time_ns));
                    Response::Telemetry(telemetry)
                }
                wire::Kind::Diagnostics => {
                    let diagnostics = wire::Diagnostics::decode(&frame.payload).map_err(lower)?;
                    if frame.step_id < self.last_ack.0
                        || frame.step_id == self.last_ack.0 && frame.time_ns < self.last_ack.1
                    {
                        return Err(state_error("co-sim diagnostics response is out of order"));
                    }
                    Response::Diagnostics(diagnostics)
                }
                wire::Kind::MotorState => {
                    let state = wire::MotorState::decode(&frame.payload).map_err(lower)?;
                    if self.last_ack.0 == 0 || record != self.last_ack {
                        return Err(state_error(
                            "co-sim MOTOR_STATE requires the latest STEP_ACK",
                        ));
                    }
                    Response::MotorState(state)
                }
                _ => {
                    return Err(state_error(
                        "input frame is invalid in QEMU co-sim response direction",
                    ))
                }
            }
        };
        Ok(Observation { frame, response })
    }
    /// Drain the existing physical suffix before one bounded read. A bad head
    /// leaves every later frame in the canonical transport, including on error.
    pub fn pump<T: Read + Write>(
        &mut self,
        stream: &mut StreamTransport<T>,
        mut observe: impl FnMut(Observation) -> Result<()>,
    ) -> Result<usize> {
        self.check()?;
        let result = (|| {
            stream.flush().map_err(lower)?;
            let mut count = 0;
            let mut read = false;
            while count < SERVICE_FRAMES {
                let body = match stream
                    .next_length_prefixed(MIN_BODY, MAX_BODY)
                    .map_err(lower)?
                {
                    Some(body) => body,
                    None if !read => {
                        stream.read_once().map_err(lower)?;
                        read = true;
                        continue;
                    }
                    None => break,
                };
                count += 1;
                if body.get(4..6) == Some(&1u16.to_le_bytes()) {
                    // Existing first-open compatibility telemetry only. Do not
                    // retain a second parsed queue or infer a v2 RESET from it.
                    let legacy = wire::V1Frame::decode_body(&body).map_err(lower)?;
                    if legacy.kind != 3 {
                        return Err(state_error("unexpected legacy co-sim frame in v2 session"));
                    }
                    continue;
                }
                let frame = wire::Frame::decode_body(&body).map_err(lower)?;
                let observation = self.accept(frame)?;
                observe(observation)?;
            }
            Ok(count)
        })();
        self.freeze(result)
    }
    /// Logical cancellation never flushes or closes the borrowed byte stream.
    pub fn close(&mut self) {
        self.closed = true;
        self.pending.clear();
        self.last_wire = None;
        self.optional_done.clear();
    }
}
