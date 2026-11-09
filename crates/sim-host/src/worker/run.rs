//! Bounded native worker orchestration. The canonical stream owns bytes,
//! CosimSession owns v2 obligations, and LegacyGraph owns plant execution.
use super::{
    engine::{EngineOptions, WorkerEngine},
    graph::LegacyGraph,
    motor_protocol::{DmAdapter, DmOptions, MotorAction, MotorFeedback},
};
use crate::{
    cosim_session::{CosimSession, Response, SessionOptions, CAP_ADC, CAP_IMU, CAP_STEP},
    endpoints::qemu_can::QemuTimestampMapper,
    step_wire::{self as wire, InputSection, StepInput, V1Frame},
    transports::{
        fixed84::{CanFrameSink, CanStream},
        stream::StreamTransport,
    },
};
use sim_core::{Error, Failure, Result, Session};
use std::{
    collections::BTreeSet,
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

const RESPONSE_TIMEOUT: Duration = Duration::from_secs(2);
const ACK_TIMEOUT: Duration = Duration::from_millis(250);
const RETRIES: usize = 3;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Protocol {
    V1,
    V2,
}
#[derive(Clone, Debug)]
pub struct WorkerOptions {
    pub cosim: PathBuf,
    pub fdcan: Option<PathBuf>,
    pub socketcan: Option<String>,
    pub protocol: Protocol,
    pub wait_step_done: bool,
    pub engine: EngineOptions,
    #[cfg(feature = "devices")]
    pub can_profile: Option<super::can_graph::CanProfile>,
    pub motors: DmOptions,
    pub rate: f64,
    pub frames: u64,
    pub adc_input: Vec<(u16, u16)>,
    pub adc_voltage: Vec<(u16, u32)>,
    pub realtime: bool,
    pub connect_timeout: Duration,
    pub host_timeout: Option<Duration>,
    pub cancellation: Option<Arc<AtomicBool>>,
    pub verbose: bool,
}
impl WorkerOptions {
    pub fn new(cosim: impl Into<PathBuf>) -> Self {
        Self {
            cosim: cosim.into(),
            fdcan: None,
            socketcan: None,
            protocol: Protocol::V1,
            wait_step_done: false,
            engine: EngineOptions::Null,
            #[cfg(feature = "devices")]
            can_profile: None,
            motors: DmOptions::default(),
            rate: 1000.0,
            frames: 0,
            adc_input: vec![],
            adc_voltage: vec![],
            realtime: false,
            connect_timeout: Duration::from_secs(10),
            host_timeout: None,
            cancellation: None,
            verbose: false,
        }
    }
    pub fn validate(&self) -> Result<()> {
        for p in std::iter::once(&self.cosim).chain(self.fdcan.iter()) {
            if p.as_os_str().is_empty() || p.as_os_str().as_encoded_bytes().contains(&0) {
                return Err(admission("worker socket path must be nonempty without NUL"));
            }
        }
        if !self.rate.is_finite()
            || self.rate <= 0.0
            || self.rate > 1_000_000_000.0
            || self.connect_timeout.is_zero()
            || self.host_timeout.is_some_and(|d| d.is_zero())
        {
            return Err(admission(
                "worker rate must be finite in (0,1GHz] and timeouts positive",
            ));
        }
        if self.wait_step_done && self.protocol != Protocol::V2 {
            return Err(admission("wait-step-done requires v2"));
        }
        if self.socketcan.is_some() && self.fdcan.is_none() {
            return Err(admission("SocketCAN forwarding requires FDCAN"));
        }
        if let Some(interface) = &self.socketcan {
            #[cfg(feature = "socketcan")]
            crate::transports::socketcan::validate_interface(interface).map_err(lower)?;
            #[cfg(not(feature = "socketcan"))]
            {
                let _ = interface;
                return Err(admission("worker SocketCAN requires socketcan feature"));
            }
        }
        if self.adc_input.len() + self.adc_voltage.len() > 32
            || self.adc_input.iter().any(|(c, _)| *c > 31)
            || self
                .adc_voltage
                .iter()
                .any(|(c, v)| *c > 31 || *v > 3_300_000)
        {
            return Err(admission(
                "worker ADC needs at most32 values, channel0..31 and voltage0..3300000uV",
            ));
        }
        self.motors.validate()?;
        #[cfg(feature = "devices")]
        if let Some(profile) = &self.can_profile {
            if self.fdcan.is_none() || !matches!(self.engine, EngineOptions::Null) {
                return Err(admission(
                    "C620 worker profile requires FDCAN and default engine",
                ));
            }
            profile.validate()?;
        }
        self.engine
            .validate(self.motors.motor_count, 1.0 / self.rate, self.motors.t_max)
    }
}
#[derive(Clone, Debug, Default)]
pub struct WorkerReport {
    pub frames: u64,
    pub sessions: u64,
    pub retries: u64,
    pub acknowledgements: u64,
    pub completions: u64,
    pub consumed_mask: u32,
    pub missing_mask: u32,
    pub final_time_ns: u64,
}
fn admission(s: &str) -> Failure {
    Arc::new(Error::Admission(s.into()))
}
fn state(s: &str) -> Failure {
    Arc::new(Error::State(s.into()))
}
fn lower(e: crate::Failure) -> Failure {
    e
}
fn timeout(phase: &str) -> Failure {
    Arc::new(Error::Deadline(format!("worker {phase} deadline exceeded")))
}

#[derive(Default)]
struct ExternalClock {
    origin: Option<u64>,
    mapped_origin: u64,
    last_external: u64,
    last_mapped: u64,
    stalled: bool,
    recovered: bool,
}
impl ExternalClock {
    fn new(origin: u64) -> Self {
        Self {
            mapped_origin: origin,
            last_mapped: origin,
            ..Default::default()
        }
    }
    fn map(&mut self, external: Option<u64>, fallback: u64) -> Result<u64> {
        let floor = self
            .last_mapped
            .checked_add(1)
            .ok_or_else(|| state("external worker clock overflow"))?;
        let fallback = fallback.max(floor);
        let mut candidate = fallback;
        if let Some(t) = external {
            if self.origin.is_none() {
                self.origin = Some(t);
                self.last_external = t;
            } else if t >= self.last_external {
                if t == self.last_external {
                    self.stalled = true;
                } else {
                    if self.stalled {
                        self.origin = Some(t);
                        self.mapped_origin = fallback;
                        self.stalled = false;
                        self.recovered = true;
                    }
                    candidate = self
                        .mapped_origin
                        .checked_add(t - self.origin.unwrap())
                        .ok_or_else(|| state("external clock exceeds u64"))?;
                    self.last_external = t;
                }
            } else {
                self.origin = Some(t);
                self.last_external = t;
                self.stalled = true;
                self.recovered = false;
            }
            if !self.stalled && !self.recovered {
                candidate = candidate.max(fallback);
            }
        } else if self.origin.is_some() {
            self.stalled = true;
            self.recovered = false;
        }
        candidate = candidate.max(floor);
        self.last_mapped = candidate;
        Ok(candidate)
    }
}

struct Worker {
    options: WorkerOptions,
    cosim: StreamTransport<UnixStream>,
    can: Option<CanStream<UnixStream>>,
    #[cfg(feature = "socketcan")]
    socketcan: Option<
        crate::transports::socketcan::SocketCanTransport<
            crate::transports::socketcan::NativeCanSocket,
        >,
    >,
    session: Option<CosimSession>,
    graph: Option<LegacyGraph>,
    #[cfg(feature = "devices")]
    can_graph: Option<super::can_graph::CanGraph>,
    #[cfg(feature = "devices")]
    can_admission: Option<super::can_graph::CanAdmission>,
    #[cfg(feature = "devices")]
    can_observer: Option<CanCallback>,
    backend: Option<Box<dyn WorkerEngine>>,
    adapter: DmAdapter,
    mapper: QemuTimestampMapper,
    origin: u64,
    time: u64,
    count: u64,
    sequence: u64,
    active: BTreeSet<usize>,
    wall_origin: Instant,
    host_deadline: Option<Instant>,
    reset_deadline: Option<Instant>,
    reset_pending: bool,
    ack: u64,
    done: u64,
    queue_full: bool,
    external: ExternalClock,
    report: WorkerReport,
}
fn check_options(options: &WorkerOptions, deadline: Option<Instant>) -> Result<()> {
    if options
        .cancellation
        .as_ref()
        .is_some_and(|c| c.load(Ordering::Relaxed))
    {
        return Err(Arc::new(Error::Interrupted("worker interrupted".into())));
    }
    if deadline.is_some_and(|d| Instant::now() >= d) {
        return Err(timeout("host"));
    }
    Ok(())
}
fn connect(
    path: &Path,
    options: &WorkerOptions,
    host_deadline: Option<Instant>,
) -> Result<UnixStream> {
    let limit = Instant::now()
        .checked_add(options.connect_timeout)
        .ok_or_else(|| admission("worker connect timeout overflows Instant"))?;
    let limit = host_deadline.map_or(limit, |d| d.min(limit));
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| Arc::new(e) as Failure)?;
    loop {
        check_options(options, Some(limit))?;
        let attempt = runtime.block_on(async {
            tokio::time::timeout_at(limit.into(), tokio::net::UnixStream::connect(path)).await
        });
        match attempt {
            Ok(Ok(stream)) => return stream.into_std().map_err(|e| Arc::new(e) as Failure),
            Ok(Err(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
                ) =>
            {
                std::thread::sleep(Duration::from_millis(1))
            }
            Ok(Err(e)) => return Err(Arc::new(e)),
            Err(_) => return Err(timeout("connect")),
        }
    }
}
impl Worker {
    fn check(&self) -> Result<()> {
        check_options(&self.options, self.host_deadline)
    }
    fn phase_deadline(&self, duration: Duration) -> Result<Instant> {
        let d = Instant::now()
            .checked_add(duration)
            .ok_or_else(|| admission("worker timeout overflow"))?;
        Ok(self.host_deadline.map_or(d, |h| h.min(d)))
    }
    fn graph_deadline(&self) -> Result<Instant> {
        self.host_deadline.map_or_else(
            || {
                Instant::now()
                    .checked_add(Duration::from_secs(u32::MAX as u64))
                    .ok_or_else(|| admission("worker deadline overflow"))
            },
            Ok,
        )
    }
    fn service(&mut self) -> Result<()> {
        self.check()?;
        if let Some(session) = &mut self.session {
            let mut events = Vec::new();
            session.pump(&mut self.cosim, |e| {
                events.push(e);
                Ok(())
            })?;
            for e in events {
                match e.response {
                    Response::Reset => {
                        if self.reset_deadline.is_none() {
                            self.reset_deadline = Some(self.phase_deadline(RESPONSE_TIMEOUT)?);
                        }
                        self.reset_pending = true;
                        self.ack = 0;
                        self.done = 0;
                    }
                    Response::StepAck(a) => {
                        if a.status == 1 {
                            self.queue_full = true;
                        } else if a.status != 0 {
                            return Err(state("native STEP_ACK rejected worker input"));
                        } else {
                            self.ack = e.frame.step_id;
                            self.queue_full = false;
                            self.report.acknowledgements += 1;
                        }
                    }
                    Response::StepDone(d) => {
                        if d.status != 0 && self.options.wait_step_done {
                            return Err(state(&format!("native STEP_DONE reported status={} consumed_mask={} missing_mask={} step={}",d.status,d.consumed_mask,d.missing_mask,e.frame.step_id)));
                        }
                        self.done = e.frame.step_id;
                        self.report.completions += 1;
                        self.report.consumed_mask |= d.consumed_mask;
                        self.report.missing_mask |= d.missing_mask;
                    }
                    Response::Telemetry(t) if self.options.verbose => eprintln!("telemetry {t:?}"),
                    _ => {}
                }
            }
        } else {
            self.cosim.flush().map_err(lower)?;
            self.cosim.read_once().map_err(lower)?;
            for _ in 0..64 {
                let Some(body) = self.cosim.next_length_prefixed(28, 156).map_err(lower)? else {
                    break;
                };
                let frame = V1Frame::decode_body(&body).map_err(lower)?;
                if self.graph.is_none() && frame.kind == 3 {
                    self.origin = self.origin.max(frame.time_ns);
                }
            }
        }
        // During RESET, arrivals stay in their physical streams until the new
        // mapper and graph epoch are admitted by RESET_ACK.
        #[cfg(feature = "devices")]
        let can_graph_live = self.can_graph.is_some();
        #[cfg(not(feature = "devices"))]
        let can_graph_live = false;
        if (self.graph.is_some() || can_graph_live) && !self.reset_pending {
            let frames = if let Some(can) = &mut self.can {
                can.transport_mut().flush().map_err(lower)?;
                can.receive().map_err(lower)?
            } else {
                vec![]
            };
            for frame in frames {
                let time = self
                    .mapper
                    .map(frame.timestamp_ns(), self.time)
                    .map_err(lower)?;
                let frame = frame.with_timestamp(time);
                #[cfg(feature = "devices")]
                if let Some(graph) = &self.can_graph {
                    graph.submit("qemu", frame)?;
                    continue;
                }
                #[cfg(feature = "socketcan")]
                if let Some(socket) = &mut self.socketcan {
                    socket.send_frame(&frame).map_err(lower)?;
                }
                if let Some(command) = self.adapter.decode(&frame) {
                    if !matches!(command.action, MotorAction::Legacy(_)) {
                        self.active.insert(command.index);
                    }
                    self.graph.as_ref().unwrap().submit(command)?;
                }
            }
            #[cfg(feature = "socketcan")]
            if let Some(socket) = &mut self.socketcan {
                socket.flush().map_err(lower)?;
                for _ in 0..64 {
                    let Some(frame) = socket.receive_once(self.time).map_err(lower)? else {
                        break;
                    };
                    #[cfg(feature = "devices")]
                    if let Some(graph) = &self.can_graph {
                        graph.submit("socketcan", frame)?;
                        continue;
                    }
                    if let Some(can) = &mut self.can {
                        let time = self
                            .mapper
                            .unmap(frame.timestamp_ns(), self.mapper.qemu_origin_ns)
                            .map_err(lower)?;
                        can.send_frame(&frame.with_timestamp(time)).map_err(lower)?;
                    }
                }
            }
        }
        self.flush_graph_outputs()?;
        self.check()
    }
    fn flush_graph_outputs(&mut self) -> Result<()> {
        #[cfg(feature = "devices")]
        if let Some(graph) = &mut self.can_graph {
            for (destination, frame) in graph.take_outputs()? {
                match destination.as_str() {
                    "qemu" => {
                        if let Some(can) = &mut self.can {
                            let time = self
                                .mapper
                                .unmap(frame.timestamp_ns(), self.mapper.qemu_origin_ns)
                                .map_err(lower)?;
                            can.send_frame(&frame.with_timestamp(time)).map_err(lower)?;
                        }
                    }
                    #[cfg(feature = "socketcan")]
                    "socketcan" => {
                        if let Some(socket) = &mut self.socketcan {
                            socket.send_frame(&frame).map_err(lower)?;
                        }
                    }
                    _ => return Err(state("unknown worker CAN transport output")),
                }
            }
        }
        Ok(())
    }
    fn wait_tick(&mut self, deadline: Instant) -> Result<()> {
        self.check()?;
        let now = Instant::now();
        if now >= deadline {
            return Err(timeout("response"));
        }
        self.wait_io((deadline - now).min(Duration::from_millis(1)))?;
        Ok(())
    }
    fn wait_io(&self, duration: Duration) -> Result<()> {
        use rustix::event::{poll, PollFd, PollFlags, Timespec};
        let flags = |pending| {
            if pending {
                PollFlags::IN | PollFlags::OUT
            } else {
                PollFlags::IN
            }
        };
        let mut peers = vec![PollFd::new(self.cosim.io(), flags(self.cosim.tx_pending()))];
        if let Some(can) = &self.can {
            peers.push(PollFd::new(
                can.transport().io(),
                flags(can.transport().tx_pending()),
            ));
        }
        let timeout = Timespec::try_from(duration).map_err(|e| Arc::new(e) as Failure)?;
        match poll(&mut peers, Some(&timeout)) {
            Ok(_) | Err(rustix::io::Errno::INTR) => Ok(()),
            Err(error) => Err(Arc::new(std::io::Error::from(error))),
        }
    }
    fn start_graph(&mut self) -> Result<()> {
        let deadline = self.graph_deadline()?;
        let id = self.session.as_ref().map_or(1, |s| s.session_id() as u64);
        if let Some(session) = &self.session {
            self.origin = session.reset_origin_ns();
            self.mapper = QemuTimestampMapper {
                qemu_origin_ns: session.native_origin_ns(),
                worker_origin_ns: self.origin,
            };
        } else {
            self.mapper = QemuTimestampMapper {
                qemu_origin_ns: self.origin,
                worker_origin_ns: self.origin,
            };
        }
        #[cfg(feature = "devices")]
        if let Some(admitted) = &self.can_admission {
            if let Some(mut old) = self.can_graph.take() {
                old.close()?;
            }
            self.can_graph = Some(super::can_graph::CanGraph::new(
                admitted,
                self.options.socketcan.is_some(),
                Session::new(id, self.origin)?,
                deadline,
                self.options.cancellation.clone(),
            )?);
        }
        #[cfg(feature = "devices")]
        let legacy = self.can_admission.is_none();
        #[cfg(not(feature = "devices"))]
        let legacy = true;
        if legacy {
            if let Some(graph) = &mut self.graph {
                graph.rebase(Session::new(id, self.origin)?, deadline)?;
            } else {
                self.graph = Some(LegacyGraph::new(
                    self.backend
                        .take()
                        .ok_or_else(|| state("worker backend missing"))?,
                    Session::new(id, self.origin)?,
                    deadline,
                    self.options.cancellation.clone(),
                )?);
            }
        }
        self.count = 0;
        self.sequence = 0;
        self.time = self.origin;
        self.wall_origin = Instant::now();
        self.active.clear();
        self.ack = 0;
        self.done = 0;
        self.queue_full = false;
        self.reset_pending = false;
        self.reset_deadline = None;
        self.external = ExternalClock::new(self.origin);
        self.report.sessions += 1;
        #[cfg(feature = "devices")]
        self.observe_can(0, self.origin)?;
        Ok(())
    }
    #[cfg(feature = "devices")]
    fn observe_can(&mut self, step_id: u64, time_ns: u64) -> Result<()> {
        if let (Some(graph), Some(observer)) = (&self.can_graph, &mut self.can_observer) {
            let id = self.session.as_ref().map_or(1, |s| s.session_id() as u64);
            observer(super::can_graph::CanObservation::new(
                graph,
                Session::new(id, time_ns)?,
                step_id,
                self.mapper,
            ))?;
        }
        Ok(())
    }
    fn complete_reset(&mut self) -> Result<()> {
        let deadline = self
            .reset_deadline
            .unwrap_or(self.phase_deadline(RESPONSE_TIMEOUT)?);
        loop {
            self.service()?;
            if self.session.as_ref().unwrap().started() {
                break;
            }
            self.wait_tick(deadline)?;
        }
        self.start_graph()
    }
    fn send_v1(&mut self, frame: V1Frame) -> Result<()> {
        let body = frame.encode_body().map_err(lower)?;
        self.cosim
            .send_length_prefixed(&body, 28, 156)
            .map_err(lower)
    }
    fn wait_step(&mut self, id: u64) -> Result<bool> {
        let admission_deadline = self.phase_deadline(RESPONSE_TIMEOUT)?;
        let mut response_deadline = Instant::now() + ACK_TIMEOUT;
        let mut retries = 0;
        loop {
            self.service()?;
            if self.reset_pending {
                self.complete_reset()?;
                return Ok(false);
            }
            if self.ack >= id {
                break;
            }
            let now = Instant::now();
            if now >= admission_deadline {
                return Err(timeout("STEP admission"));
            }
            if self.queue_full || now >= response_deadline {
                if !self.queue_full {
                    if retries >= RETRIES {
                        return Err(timeout("STEP_ACK"));
                    }
                    retries += 1;
                }
                self.session.as_mut().unwrap().retry_last(&mut self.cosim)?;
                self.report.retries += 1;
                self.queue_full = false;
                response_deadline = now + ACK_TIMEOUT;
            }
            self.wait_tick(admission_deadline)?;
        }
        if self.options.wait_step_done {
            let mut deadline = self.phase_deadline(RESPONSE_TIMEOUT)?;
            let mut retries = 0;
            loop {
                self.service()?;
                if self.reset_pending {
                    self.complete_reset()?;
                    return Ok(false);
                }
                if self.done >= id {
                    break;
                }
                if Instant::now() >= deadline {
                    if retries >= RETRIES {
                        return Err(timeout("STEP_DONE"));
                    }
                    retries += 1;
                    self.session.as_mut().unwrap().retry_last(&mut self.cosim)?;
                    self.report.retries += 1;
                    deadline = self.phase_deadline(RESPONSE_TIMEOUT)?;
                }
                // Response timeouts trigger replay above; the host deadline and
                // cancellation continue to bound every pump and sleep.
                self.wait_tick(deadline)?;
            }
        }
        Ok(true)
    }
    fn execute(&mut self) -> Result<WorkerReport> {
        if self.options.protocol == Protocol::V2 {
            self.session
                .as_mut()
                .unwrap()
                .begin_reset(&mut self.cosim)?;
            self.complete_reset()?;
        } else {
            let grace = self.phase_deadline(Duration::from_millis(20))?;
            while Instant::now() < grace {
                self.service()?;
                std::thread::sleep(Duration::from_millis(1));
            }
            self.send_v1(V1Frame {
                kind: 1,
                sequence: 0,
                time_ns: self.origin,
                payload: vec![],
            })?;
            self.start_graph()?;
        }
        let dt = 1.0 / self.options.rate;
        while self.options.frames == 0 || self.count < self.options.frames {
            self.service()?;
            if self.reset_pending {
                self.complete_reset()?;
                continue;
            }
            let deadline = self.graph_deadline()?;
            let sample = if let Some(graph) = &mut self.graph {
                graph.advance(dt, deadline)?
            } else {
                super::engine::EngineSample {
                    imu: crate::signals::ImuSample {
                        gyro_rad_s: [0.0; 3],
                        accel_m_s2: [0.0, 0.0, 9.80665],
                    },
                    source_time_ns: None,
                    motors: vec![],
                }
            };
            let index = self
                .count
                .checked_add(1)
                .ok_or_else(|| state("worker frame count overflow"))?;
            let offset = if self.options.rate.fract() == 0.0 {
                let n = index as u128 * 1_000_000_000u128;
                let rate = self.options.rate as u128;
                let q = n / rate;
                let r = n % rate;
                q + u128::from(r * 2 > rate || r * 2 == rate && !q.is_multiple_of(2))
            } else {
                let v = (index as f64 * 1_000_000_000.0 / self.options.rate).round_ties_even();
                if v >= u64::MAX as f64 {
                    return Err(state("worker sample timestamp overflow"));
                }
                v as u128
            };
            let offset =
                u64::try_from(offset).map_err(|_| state("worker timestamp exceeds u64"))?;
            let ideal = self
                .origin
                .checked_add(offset)
                .ok_or_else(|| state("worker timestamp overflow"))?;
            let ideal = if matches!(&self.options.engine, EngineOptions::Null) {
                ideal
            } else {
                self.external.map(sample.source_time_ns, ideal)?
            };
            let subticks = if self.options.protocol == Protocol::V1 {
                self.options.adc_input.len() + self.options.adc_voltage.len() + 1
            } else {
                1
            };
            let target = ideal.max(
                self.time
                    .checked_add(subticks as u64)
                    .ok_or_else(|| state("worker subtick overflow"))?,
            );
            if let Some(graph) = &mut self.graph {
                graph.reach(target, deadline)?;
            }
            #[cfg(feature = "devices")]
            let sample = {
                let mut sample = sample;
                if let Some(graph) = &mut self.can_graph {
                    graph.reach(target, deadline)?;
                    sample.imu = graph.sample()?;
                }
                sample
            };
            #[cfg(feature = "devices")]
            self.observe_can(index, target)?;
            self.flush_graph_outputs()?;
            let (gyro, accel) = crate::devices::bmi088::imu_to_bmi088_units(&sample.imu)?;
            if self.options.protocol == Protocol::V1 {
                let raw = self.options.adc_input.clone();
                let volts = self.options.adc_voltage.clone();
                let mut subtime = self.time;
                for (c, v) in raw {
                    subtime += 1;
                    self.sequence += 1;
                    self.send_v1(V1Frame {
                        kind: 5,
                        sequence: self.sequence,
                        time_ns: subtime,
                        payload: wire::AdcInput { channel: c, raw: v }
                            .encode()
                            .map_err(lower)?,
                    })?;
                }
                for (c, v) in volts {
                    subtime += 1;
                    self.sequence += 1;
                    self.send_v1(V1Frame {
                        kind: 6,
                        sequence: self.sequence,
                        time_ns: subtime,
                        payload: wire::AdcVoltage {
                            channel: c,
                            flags: 0,
                            voltage_uv: v,
                        }
                        .encode()
                        .map_err(lower)?,
                    })?;
                }
                self.sequence += 1;
                let payload = gyro
                    .into_iter()
                    .chain(accel)
                    .flat_map(f32::to_le_bytes)
                    .collect();
                self.send_v1(V1Frame {
                    kind: 2,
                    sequence: self.sequence,
                    time_ns: target,
                    payload,
                })?;
            } else {
                let imu = wire::ImuSample {
                    gyro_dps: gyro,
                    accel_g: accel,
                    gyro_bias_dps: [0.0; 3],
                    accel_bias_g: [0.0; 3],
                    sensor_time_ns: target,
                    sample_id: u32::try_from(index)
                        .map_err(|_| state("worker IMU sample ID exceeds u32"))?,
                };
                let input =
                    if self.options.adc_input.is_empty() && self.options.adc_voltage.is_empty() {
                        StepInput::CompactImu(imu)
                    } else {
                        let mut sections = vec![InputSection::Imu(imu)];
                        sections.extend(self.options.adc_input.iter().map(|(c, v)| {
                            InputSection::Adc(wire::AdcInput {
                                channel: *c,
                                raw: *v,
                            })
                        }));
                        sections.extend(self.options.adc_voltage.iter().map(|(c, v)| {
                            InputSection::Voltage(wire::AdcVoltage {
                                channel: *c,
                                flags: 0,
                                voltage_uv: *v,
                            })
                        }));
                        StepInput::Sections(sections)
                    };
                let id = self
                    .session
                    .as_mut()
                    .unwrap()
                    .submit(&mut self.cosim, target, input)?;
                // CAN received while waiting is scheduled at this completed cut.
                self.time = target;
                if !self.wait_step(id)? {
                    continue;
                }
            }
            self.time = target;
            if let Some(can) = &mut self.can {
                for &i in &self.active {
                    if let Some(&(p, v, t)) = sample.motors.get(i) {
                        let frame = self.adapter.feedback(MotorFeedback {
                            index: i,
                            position: p,
                            velocity: v,
                            effort: t,
                            timestamp_ns: target,
                        })?;
                        let stamp = self
                            .mapper
                            .unmap(target, self.mapper.qemu_origin_ns)
                            .map_err(lower)?;
                        can.send_frame(&frame.with_timestamp(stamp))
                            .map_err(lower)?;
                    }
                }
            }
            self.count += 1;
            self.report.frames += 1;
            self.report.final_time_ns = target;
            if self.options.realtime {
                let delay = Duration::from_nanos(target - self.origin);
                let end = self
                    .wall_origin
                    .checked_add(delay)
                    .ok_or_else(|| state("worker realtime target overflow"))?;
                while Instant::now() < end {
                    self.service()?;
                    self.wait_io(
                        end.saturating_duration_since(Instant::now())
                            .min(Duration::from_millis(1)),
                    )?;
                }
            }
        }
        let deadline = self.phase_deadline(RESPONSE_TIMEOUT)?;
        while self.cosim.tx_pending()
            || self
                .can
                .as_ref()
                .is_some_and(|c| c.transport().tx_pending())
        {
            self.service()?;
            if Instant::now() >= deadline {
                return Err(timeout("drain"));
            }
            self.wait_tick(deadline)?;
        }
        #[cfg(feature = "socketcan")]
        if let Some(socket) = &mut self.socketcan {
            while !socket.flush().map_err(lower)? {
                if Instant::now() >= deadline {
                    return Err(timeout("SocketCAN drain"));
                }
                check_options(&self.options, self.host_deadline)?;
                std::thread::sleep(Duration::from_millis(1));
            }
        }
        self.check()?;
        Ok(self.report.clone())
    }
    fn close(&mut self) -> Result<()> {
        let mut primary = None;
        if let Some(g) = &mut self.graph {
            primary = g.close().err();
        }
        #[cfg(feature = "devices")]
        if let Some(g) = &mut self.can_graph {
            if let Err(error) = g.close() {
                if primary.is_none() {
                    primary = Some(error)
                } else {
                    eprintln!("worker CAN graph cleanup: {error}");
                }
            }
        }
        if let Some(mut b) = self.backend.take() {
            if let Err(e) = b.close() {
                if primary.is_none() {
                    primary = Some(e)
                }
            }
        }
        if let Some(s) = &mut self.session {
            s.close();
        }
        primary.map_or(Ok(()), Err)
    }
}

/// Runs to the requested frame count (zero means until interrupted). Success is
/// returned only after graph/backend cleanup; socket owners then drop by RAII.
pub fn run(options: WorkerOptions) -> Result<WorkerReport> {
    run_using(options, false, RunHooks::default(), |options| {
        options.engine.create(
            options.motors.motor_count,
            1.0 / options.rate,
            options.motors.t_max,
        )
    })
}

/// Supplies one compiled engine for a direct consumer while retaining the same
/// worker transport, deadlines and cleanup owner. C620 graphs own their plant
/// and therefore cannot be combined with an injected legacy engine.
pub fn run_with_engine(
    options: WorkerOptions,
    factory: impl FnOnce(&WorkerOptions) -> Result<Box<dyn WorkerEngine>>,
) -> Result<WorkerReport> {
    run_using(options, true, RunHooks::default(), factory)
}

#[cfg(feature = "devices")]
type CanCallback = Box<dyn for<'a> FnMut(super::can_graph::CanObservation<'a>) -> Result<()>>;

#[derive(Default)]
struct RunHooks {
    #[cfg(feature = "devices")]
    can_observer: Option<CanCallback>,
}

/// Observes completed C620 cuts without granting plant or transport ownership.
/// An observer error terminates the worker and follows its normal cleanup path.
#[cfg(feature = "devices")]
pub fn run_with_can_observer(
    options: WorkerOptions,
    callback: impl for<'a> FnMut(super::can_graph::CanObservation<'a>) -> Result<()> + 'static,
) -> Result<WorkerReport> {
    run_using(
        options,
        false,
        RunHooks {
            can_observer: Some(Box::new(callback)),
        },
        |options| {
            options.engine.create(
                options.motors.motor_count,
                1.0 / options.rate,
                options.motors.t_max,
            )
        },
    )
}

fn run_using(
    options: WorkerOptions,
    injected: bool,
    hooks: RunHooks,
    factory: impl FnOnce(&WorkerOptions) -> Result<Box<dyn WorkerEngine>>,
) -> Result<WorkerReport> {
    options.validate()?;
    #[cfg(feature = "devices")]
    if hooks.can_observer.is_some() && options.can_profile.is_none() {
        return Err(admission("CAN observation requires a C620 profile"));
    }
    #[cfg(not(feature = "devices"))]
    let _ = hooks;
    #[cfg(feature = "devices")]
    if injected && options.can_profile.is_some() {
        return Err(admission(
            "injected legacy engine cannot own a C620 profile",
        ));
    }
    #[cfg(not(feature = "devices"))]
    let _ = injected;
    let start = Instant::now();
    let host_deadline = options
        .host_timeout
        .map(|d| {
            start
                .checked_add(d)
                .ok_or_else(|| admission("worker host timeout overflow"))
        })
        .transpose()?;
    check_options(&options, host_deadline)?;
    let capabilities = CAP_STEP
        | CAP_IMU
        | if options.adc_input.is_empty() && options.adc_voltage.is_empty() {
            0
        } else {
            CAP_ADC
        };
    let session = if options.protocol == Protocol::V2 {
        Some(CosimSession::new_worker(
            SessionOptions {
                session_id: CosimSession::generate_session_id()?,
                sim_origin_ns: 0,
                native_origin_ns: 0,
                required_capabilities: capabilities,
            },
            options.wait_step_done,
        )?)
    } else {
        None
    };
    let adapter = DmAdapter::new(options.motors.clone())?;
    #[cfg(feature = "devices")]
    let can_admission = options
        .can_profile
        .as_ref()
        .map(|profile| {
            profile.admit(
                options.socketcan.is_some(),
                host_deadline.unwrap_or(Instant::now() + options.connect_timeout),
                options.cancellation.clone(),
            )
        })
        .transpose()?;
    #[cfg(feature = "devices")]
    let legacy = can_admission.is_none();
    #[cfg(not(feature = "devices"))]
    let legacy = true;
    let mut backend = if legacy {
        Some(factory(&options)?)
    } else {
        None
    };
    let acquired = (|| -> Result<_> {
        let cosim = StreamTransport::new(connect(&options.cosim, &options, host_deadline)?, 256)
            .map_err(lower)?;
        let can = options
            .fdcan
            .as_ref()
            .map(|p| {
                connect(p, &options, host_deadline)
                    .and_then(|s| CanStream::new(s, 256).map_err(lower))
            })
            .transpose()?;
        Ok((cosim, can))
    })();
    let (cosim, can) = match acquired {
        Ok(v) => v,
        Err(primary) => {
            if let Some(b) = &mut backend {
                if let Err(e) = b.close() {
                    eprintln!("worker backend cleanup: {e}");
                }
            }
            return Err(primary);
        }
    };
    #[cfg(feature = "socketcan")]
    let socketcan = match options
        .socketcan
        .as_ref()
        .map(|i| {
            crate::transports::socketcan::NativeCanSocket::open(i, true).and_then(|s| {
                crate::transports::socketcan::SocketCanTransport::new(s, true, 256, 64)
            })
        })
        .transpose()
    {
        Ok(v) => v,
        Err(primary) => {
            if let Some(b) = &mut backend {
                if let Err(e) = b.close() {
                    eprintln!("worker backend cleanup: {e}");
                }
            }
            return Err(primary);
        }
    };
    let mut worker = Worker {
        options,
        cosim,
        can,
        #[cfg(feature = "socketcan")]
        socketcan,
        session,
        graph: None,
        backend,
        #[cfg(feature = "devices")]
        can_graph: None,
        #[cfg(feature = "devices")]
        can_admission,
        #[cfg(feature = "devices")]
        can_observer: hooks.can_observer,
        adapter,
        mapper: QemuTimestampMapper {
            qemu_origin_ns: 0,
            worker_origin_ns: 0,
        },
        origin: 0,
        time: 0,
        count: 0,
        sequence: 0,
        active: BTreeSet::new(),
        wall_origin: Instant::now(),
        host_deadline,
        reset_deadline: None,
        reset_pending: false,
        ack: 0,
        done: 0,
        queue_full: false,
        external: ExternalClock::default(),
        report: WorkerReport::default(),
    };
    let result = worker.execute();
    let cleanup = worker.close();
    match (result, cleanup) {
        (Err(primary), Err(e)) => {
            eprintln!("worker cleanup: {e}");
            Err(primary)
        }
        (Err(e), _) | (_, Err(e)) => Err(e),
        (Ok(report), Ok(())) => Ok(report),
    }
}
