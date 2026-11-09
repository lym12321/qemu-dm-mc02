//! Narrow port of tools/dm_mc02_motor_adapter.py's existing DM MIT/float mapping.
//! Quantization and byte order remain independent of plant and transport owners.
use super::motor_policy::{MitCommand, MotorPolicy};
use crate::can::{CanFrame, FD, RTR};
use sim_core::{Error, Failure, Result};
use std::{collections::BTreeSet, sync::Arc};
fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MotorProtocol {
    Auto,
    Float,
    DmMit,
}
#[derive(Clone, Debug)]
pub struct DmOptions {
    pub motor_count: usize,
    pub protocol: MotorProtocol,
    pub motor_can_base: u32,
    pub slave_id: u32,
    pub feedback_id: u32,
    pub motor_map: Vec<(usize, u32, u32)>,
    pub p_max: f64,
    pub v_max: f64,
    pub t_max: f64,
}
impl Default for DmOptions {
    fn default() -> Self {
        Self {
            motor_count: 4,
            protocol: MotorProtocol::Auto,
            motor_can_base: 0x200,
            slave_id: 1,
            feedback_id: 0x11,
            motor_map: vec![],
            p_max: 12.5,
            v_max: 30.0,
            t_max: 10.0,
        }
    }
}
impl DmOptions {
    pub fn validate(&self) -> Result<()> {
        if self.motor_count > 1024
            || self.motor_can_base > 0x7ff
            || ![self.p_max, self.v_max, self.t_max]
                .iter()
                .all(|v| v.is_finite() && *v > 0.0)
        {
            return Err(admission(
                "DM requires bounded motor count, standard base ID and finite positive limits",
            ));
        }
        let mut indices = BTreeSet::new();
        for (index, _, _) in &self.motor_map {
            if *index >= self.motor_count || !indices.insert(index) {
                return Err(admission(
                    "DM override indexes must be unique within motor count",
                ));
            }
        }
        let mut ids = BTreeSet::new();
        for index in 0..self.motor_count {
            let (a, b) = self.ids(index);
            if a > 0x7ff || b > 0x7ff || !ids.insert(a) || !ids.insert(b) {
                return Err(admission(
                    "effective DM control/feedback IDs must be unique standard identifiers",
                ));
            }
        }
        Ok(())
    }
    fn ids(&self, index: usize) -> (u32, u32) {
        self.motor_map
            .iter()
            .find(|(i, _, _)| *i == index)
            .map(|(_, a, b)| (*a, *b))
            .unwrap_or((
                self.slave_id.saturating_add(index as u32),
                self.feedback_id.saturating_add(index as u32),
            ))
    }
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum MotorAction {
    Legacy(f64),
    Reset,
    Enable(bool),
    Mit(MitCommand),
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MotorCommand {
    pub index: usize,
    pub action: MotorAction,
    pub timestamp_ns: u64,
}
impl MotorCommand {
    pub fn apply(&self, policy: &mut MotorPolicy) -> Result<()> {
        match self.action {
            MotorAction::Legacy(value) => policy.set_legacy(value),
            MotorAction::Mit(command) => policy.set_mit(command),
            MotorAction::Reset => {
                policy.reset();
                Ok(())
            }
            MotorAction::Enable(enabled) => {
                policy.set_enabled(enabled);
                Ok(())
            }
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MotorFeedback {
    pub index: usize,
    pub position: f64,
    pub velocity: f64,
    pub effort: f64,
    pub timestamp_ns: u64,
}
pub struct DmAdapter {
    options: DmOptions,
}
fn decode_linear(raw: u32, bits: u32, max: f64) -> f64 {
    raw as f64 * (2.0 * max) / ((1u32 << bits) - 1) as f64 - max
}
fn encode_linear(value: f64, bits: u32, max: f64) -> u32 {
    ((value.clamp(-max, max) + max) * ((1u32 << bits) - 1) as f64 / (2.0 * max)).round_ties_even()
        as u32
}
impl DmAdapter {
    pub fn new(options: DmOptions) -> Result<Self> {
        options.validate()?;
        Ok(Self { options })
    }
    pub fn options(&self) -> &DmOptions {
        &self.options
    }
    pub fn decode(&self, frame: &CanFrame) -> Option<MotorCommand> {
        if frame.flags() & (RTR | FD) != 0 {
            return None;
        }
        let data = frame.data();
        if self.options.protocol != MotorProtocol::Float {
            let index = self
                .options
                .motor_map
                .iter()
                .find(|(_, id, _)| *id == frame.can_id())
                .map(|(i, _, _)| *i)
                .or_else(|| {
                    frame
                        .can_id()
                        .checked_sub(self.options.slave_id)
                        .map(|i| i as usize)
                        .filter(|i| *i < self.options.motor_count)
                });
            if let Some(index) = index.filter(|_| data.len() == 8) {
                let action = if data[..7] == [0xff; 7] && [0xfb, 0xfc, 0xfd].contains(&data[7]) {
                    match data[7] {
                        0xfb => MotorAction::Reset,
                        0xfc => MotorAction::Enable(true),
                        _ => MotorAction::Enable(false),
                    }
                } else {
                    let p = u16::from_be_bytes([data[0], data[1]]) as u32;
                    let v = ((data[2] as u32) << 4) | ((data[3] as u32) >> 4);
                    let kp = (((data[3] & 15) as u32) << 8) | data[4] as u32;
                    let kd = ((data[5] as u32) << 4) | ((data[6] as u32) >> 4);
                    let t = (((data[6] & 15) as u32) << 8) | data[7] as u32;
                    MotorAction::Mit(MitCommand {
                        position: decode_linear(p, 16, self.options.p_max),
                        speed: decode_linear(v, 12, self.options.v_max),
                        kp: kp as f64 * 500.0 / 4095.0,
                        kd: kd as f64 * 5.0 / 4095.0,
                        torque: decode_linear(t, 12, self.options.t_max),
                    })
                };
                return Some(MotorCommand {
                    index,
                    action,
                    timestamp_ns: frame.timestamp_ns(),
                });
            }
        }
        if self.options.protocol != MotorProtocol::DmMit && data.len() >= 4 {
            let index = frame.can_id().checked_sub(self.options.motor_can_base)? as usize;
            if index >= self.options.motor_count {
                return None;
            }
            let value = f32::from_le_bytes(data[..4].try_into().expect("four bytes")) as f64;
            if value.is_finite() {
                return Some(MotorCommand {
                    index,
                    action: MotorAction::Legacy(value),
                    timestamp_ns: frame.timestamp_ns(),
                });
            }
        }
        None
    }
    pub fn feedback(&self, state: MotorFeedback) -> Result<CanFrame> {
        if state.index >= self.options.motor_count
            || ![state.position, state.velocity, state.effort]
                .iter()
                .all(|v| v.is_finite())
        {
            return Err(admission(
                "DM feedback needs valid index and finite observations",
            ));
        }
        let (control, id) = self.options.ids(state.index);
        let p = encode_linear(state.position, 16, self.options.p_max);
        let v = encode_linear(state.velocity, 12, self.options.v_max);
        let t = encode_linear(state.effort, 12, self.options.t_max);
        CanFrame::new(
            id,
            0,
            None,
            vec![
                (control & 15) as u8,
                (p >> 8) as u8,
                p as u8,
                (v >> 4) as u8,
                (((v & 15) << 4) | (t >> 8)) as u8,
                t as u8,
                25,
                25,
            ],
            state.timestamp_ns,
        )
        .map_err(|error| error as Failure)
    }
}
