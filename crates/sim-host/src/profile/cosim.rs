//! Stages one completed ADC/IMU cut over the existing logical v2 session. The
//! machine owns the socket, process, native clock and host deadline.
use super::MachineInput;
use crate::{
    machine::{
        admission,
        cosim_session::{
            CosimSession, Observation, Response, SessionOptions, CAP_ADC, CAP_IMU, CAP_STEP,
        },
        state_error, NativeBackend,
    },
    runtime::{HostContext, HostEvent},
    signals::ImuSignalSample,
    step_wire as wire,
    transports::stream::StreamTransport,
};
use serde_json::{json, Value};
use sim_core::{EventTime, Result, TimeQuality};
use std::{collections::BTreeMap, os::unix::net::UnixStream};

pub(super) fn adc_channel(port: &str) -> Result<u16> {
    let channel = port
        .strip_prefix("adc1.ch")
        .and_then(|s| s.parse::<u16>().ok())
        .filter(|channel| *channel < 32 && port == format!("adc1.ch{channel}"));
    channel.ok_or_else(|| admission("ADC endpoint requires adc1.ch0..31"))
}
pub(super) fn adc_section(
    channel: u16,
    input: &MachineInput,
) -> Result<(wire::InputSection, Value)> {
    let (section, value) = match *input {
        MachineInput::AdcRaw(raw) => (
            wire::InputSection::Adc(wire::AdcInput { channel, raw }),
            json!({"raw":raw}),
        ),
        MachineInput::AdcVoltage(voltage_uv) => (
            wire::InputSection::Voltage(wire::AdcVoltage {
                channel,
                flags: 0,
                voltage_uv,
            }),
            json!({"voltage_uv":voltage_uv}),
        ),
        _ => return Err(admission("ADC input requires exactly raw or voltage_uv")),
    };
    wire::StepInput::Sections(vec![section.clone()])
        .encode()
        .map_err(super::lower)?;
    Ok((section, value))
}
struct Staged {
    cut: u64,
    adc: BTreeMap<u16, (wire::InputSection, Value)>,
    imu: Option<(wire::ImuSample, Value)>,
}
struct Pending {
    time: u64,
    dt: u64,
    adc: BTreeMap<u16, Value>,
    imu: Option<Value>,
}
pub(super) struct CosimEndpoint {
    context: HostContext,
    session: CosimSession,
    staged: Option<Staged>,
    pending: BTreeMap<u64, Pending>,
    imu_enabled: bool,
    initial_seen: bool,
}
impl CosimEndpoint {
    pub(super) fn start(
        context: HostContext,
        backend: &mut NativeBackend,
        stream: &mut StreamTransport<UnixStream>,
        imu_enabled: bool,
        adc_enabled: bool,
    ) -> Result<Self> {
        let mut endpoint = Self {
            session: CosimSession::new(SessionOptions {
                session_id: CosimSession::generate_session_id()?,
                sim_origin_ns: backend.t_sim_ns(),
                native_origin_ns: backend.native_origin_ns()?,
                required_capabilities: CAP_STEP
                    | if imu_enabled { CAP_IMU } else { 0 }
                    | if adc_enabled { CAP_ADC } else { 0 },
            })?,
            context,
            staged: None,
            pending: BTreeMap::new(),
            imu_enabled,
            initial_seen: false,
        };
        backend.check()?;
        endpoint.session.begin_reset(stream)?;
        while !endpoint.session.started() {
            endpoint.service(backend, stream, backend.t_sim_ns())?;
            if !endpoint.session.started() {
                backend.wait_poll()?;
            }
        }
        Ok(endpoint)
    }
    fn stage_cut(&mut self, cut: u64) -> Result<&mut Staged> {
        self.context.check_io()?;
        self.context.check_deadline()?;
        if cut <= self.session.last_submission().1 {
            return Err(admission(
                "co-sim input cut must increase strictly beyond session origin",
            ));
        }
        if self.staged.as_ref().is_some_and(|staged| staged.cut != cut) {
            return Err(state_error(
                "previous co-sim input cut has not been committed",
            ));
        }
        Ok(self.staged.get_or_insert_with(|| Staged {
            cut,
            adc: BTreeMap::new(),
            imu: None,
        }))
    }
    pub(super) fn stage_adc(&mut self, channel: u16, input: &MachineInput, cut: u64) -> Result<()> {
        let value = adc_section(channel, input)?;
        let staged = self.stage_cut(cut)?;
        if staged.adc.contains_key(&channel) {
            return Err(admission("duplicate ADC channel at one cut"));
        }
        if staged.adc.len() >= 16 {
            return Err(admission("ADC channel capacity per cut is 16"));
        }
        staged.adc.insert(channel, value);
        Ok(())
    }
    pub(super) fn stage_imu(&mut self, envelope: ImuSignalSample, cut: u64) -> Result<()> {
        self.context.check_io()?;
        self.context.check_deadline()?;
        envelope.validate()?;
        if !self.imu_enabled || envelope.mapped_time_ns != cut {
            return Err(admission(
                "IMU input must be enabled and match the completed cut",
            ));
        }
        let source = json!({"gyro_rad_s":envelope.sample.gyro_rad_s,"accel_m_s2":envelope.sample.accel_m_s2,"source_time_ns":envelope.source_time_ns,"mapped_time_ns":cut});
        if cut == self.context.session()?.t_sim_ns
            && !self.initial_seen
            && self.session.last_submission().0 == 0
        {
            self.initial_seen = true;
            return emit(
                &self.context,
                Some("imu"),
                "imu-input-initial",
                json!({"input":source}),
                cut,
                cut,
            );
        }
        let (gyro_dps, accel_g) = crate::devices::bmi088::imu_to_bmi088_units(&envelope.sample)?;
        let input = wire::ImuSample {
            gyro_dps,
            accel_g,
            gyro_bias_dps: [0.; 3],
            accel_bias_g: [0.; 3],
            sensor_time_ns: cut,
            sample_id: 0,
        };
        input.encode().map_err(super::lower)?;
        let staged = self.stage_cut(cut)?;
        if staged.imu.is_some() {
            return Err(admission("duplicate IMU input at one cut"));
        }
        staged.imu = Some((input, source));
        Ok(())
    }
    pub(super) fn commit(
        &mut self,
        backend: &mut NativeBackend,
        stream: &mut StreamTransport<UnixStream>,
        cut: u64,
    ) -> Result<()> {
        let Some(staged) = self.staged.as_ref() else {
            return Ok(());
        };
        if staged.cut != cut {
            return Err(state_error("co-sim commit does not match staged cut"));
        }
        let mut sections = Vec::new();
        if let Some((imu, _)) = &staged.imu {
            sections.push(wire::InputSection::Imu(imu.clone()));
        }
        sections.extend(staged.adc.values().map(|(section, _)| section.clone()));
        let input = wire::StepInput::Sections(sections);
        let record = Pending {
            time: cut,
            dt: cut - self.session.last_submission().1,
            adc: staged
                .adc
                .iter()
                .map(|(channel, (_, value))| (*channel, value.clone()))
                .collect(),
            imu:staged.imu.as_ref().map(|(wire,input)|json!({"input":input,"wire":{"gyro_dps":wire.gyro_dps,"accel_g":wire.accel_g,"gyro_bias_dps":wire.gyro_bias_dps,"accel_bias_g":wire.accel_bias_g,"sensor_time_ns":wire.sensor_time_ns,"sample_id":wire.sample_id}})),
        };
        self.service(backend, stream, cut)?;
        backend.check()?;
        let id = self.session.submit(stream, cut, input)?;
        self.pending.insert(id, record);
        while !self.session.acknowledged(id) {
            self.service(backend, stream, cut)?;
            if !self.session.acknowledged(id) {
                backend.wait_poll()?;
            }
        }
        self.staged = None;
        backend.check()
    }
    pub(super) fn service(
        &mut self,
        backend: &mut NativeBackend,
        stream: &mut StreamTransport<UnixStream>,
        cut: u64,
    ) -> Result<()> {
        backend.check()?;
        let id = self.session.session_id();
        let context = &self.context;
        let pending = &mut self.pending;
        let imu_enabled = self.imu_enabled;
        self.session.pump(stream, |observation| {
            publish(context, pending, id, imu_enabled, observation, cut)
        })?;
        backend.check()
    }
    pub(super) fn close(&mut self) {
        self.session.close();
    }
}
fn emit(
    context: &HostContext,
    port: Option<&str>,
    kind: &str,
    value: Value,
    cut: u64,
    mapped: u64,
) -> Result<()> {
    context.emit(
        port,
        kind,
        HostEvent::Cosim { value },
        EventTime {
            source_time_ns: None,
            mapped_time_ns: Some(mapped),
            delivery_time_ns: Some(cut),
            quality: Some(TimeQuality::Sampled),
        },
    )?;
    Ok(())
}
fn publish(
    context: &HostContext,
    pending: &mut BTreeMap<u64, Pending>,
    session_id: u32,
    imu_enabled: bool,
    observation: Observation,
    cut: u64,
) -> Result<()> {
    let header = &observation.frame;
    let (kind, response) = match observation.response {
        Response::Reset => return Err(state_error("unexpected RESET in strict profile session")),
        Response::ResetAck(_) => return Ok(()),
        Response::StepAck(ack) => {
            let record = pending
                .get(&header.step_id)
                .ok_or_else(|| state_error("co-sim ACK metadata is missing"))?;
            if record.imu.is_some() {
                let mut value = record_value(record, session_id, header.step_id);
                value.as_object_mut().unwrap().extend(json!({"status":ack.status,"queue_depth":ack.queue_depth,"dropped_count":ack.dropped_count}).as_object().unwrap().clone());
                emit(
                    context,
                    Some("imu"),
                    "imu-input-accepted",
                    value,
                    cut,
                    record.time,
                )?;
            }
            for (channel, input) in &record.adc {
                emit(
                    context,
                    Some(&format!("adc1.ch{channel}")),
                    "adc-input-accepted",
                    json!({"session_id":session_id,"step_id":header.step_id,"t_sim_ns":record.time,"dt_ns":record.dt,"input":input,"status":ack.status,"queue_depth":ack.queue_depth,"dropped_count":ack.dropped_count}),
                    cut,
                    record.time,
                )?;
            }
            if record.imu.is_none() {
                pending.remove(&header.step_id);
            }
            return Ok(());
        }
        Response::StepDone(done) => {
            let record = pending
                .remove(&header.step_id)
                .filter(|record| record.imu.is_some())
                .ok_or_else(|| state_error("IMU DONE metadata is missing"))?;
            let mut value = record_value(&record, session_id, header.step_id);
            value.as_object_mut().unwrap().extend(json!({"status":done.status,"consumed_mask":done.consumed_mask,"missing_mask":done.missing_mask,"queue_depth":done.queue_depth,"dropped_count":done.dropped_count}).as_object().unwrap().clone());
            return emit(
                context,
                Some("imu"),
                "imu-consumption",
                value,
                cut,
                record.time,
            );
        }
        Response::Telemetry(v) => (
            "cosim-telemetry",
            json!({"led_rgb":v.led_rgb,"led_brightness":v.led_brightness,"buzzer":v.buzzer,"board_flags":v.board_flags,"buzzer_frequency_hz":v.buzzer_frequency_hz,"buzzer_duty_permille":v.buzzer_duty_permille}),
        ),
        Response::Diagnostics(v) => (
            "cosim-diagnostics",
            json!({"rx_frames":v.rx_frames,"rx_bad_frames":v.rx_bad_frames,"rx_dropped_bytes":v.rx_dropped_bytes,"tx_frames":v.tx_frames,"tx_dropped":v.tx_dropped}),
        ),
        Response::MotorState(v) => (
            "cosim-motor-state",
            json!({"states":v.states.into_iter().map(|v|json!({"index":v.index,"position":v.position,"velocity":v.velocity,"effort":v.effort})).collect::<Vec<_>>()}),
        ),
    };
    emit(
        context,
        imu_enabled.then_some("imu"),
        &if imu_enabled {
            kind.replace("cosim-", "imu-")
        } else {
            kind.into()
        },
        json!({"header":{"version":2,"kind":header.kind as u16,"payload_len":header.payload.len(),"step_id":header.step_id,"t_sim_ns":header.time_ns,"dt_ns":header.dt_ns,"flags":header.session_id},"response":response}),
        cut,
        cut,
    )
}
fn record_value(record: &Pending, session_id: u32, id: u64) -> Value {
    let mut value =
        json!({"session_id":session_id,"step_id":id,"t_sim_ns":record.time,"dt_ns":record.dt});
    if let Some(imu) = &record.imu {
        value
            .as_object_mut()
            .unwrap()
            .extend(imu.as_object().unwrap().clone());
    }
    value
}
