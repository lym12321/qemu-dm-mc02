//! Four outgoing fixed-2026 V1.1.0 payloads (20251217), official pp9–10/14–17.
//! Wire fields only. Buff uses packed struct length8/final offset7; the table's
//! conflicting offset6 is not adopted. No HP, rule, clock or sequence policy.

use super::protocol::encode_frame;
use sim_core::{Error, Result};
use std::sync::Arc;
fn admission(message: &str) -> sim_core::Failure {
    Arc::new(Error::Admission(message.into()))
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct GameStatus {
    pub game_type: u8,
    pub game_progress: u8,
    pub stage_remain_time: u16,
    pub sync_timestamp: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RobotStatus {
    pub robot_id: u8,
    pub robot_level: u8,
    pub current_hp: u16,
    pub maximum_hp: u16,
    pub cooling_per_s: u16,
    pub heat_limit: u16,
    pub chassis_power_limit: u16,
    pub power_outputs: u8,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PowerHeat {
    pub buffer_energy_j: u16,
    pub heat_17mm: u16,
    pub heat_42mm: u16,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Buff {
    pub recovery_percent: u8,
    pub cooling_per_s: u16,
    pub defence_percent: u8,
    pub vulnerability_percent: u8,
    pub attack_percent: u16,
    pub remaining_energy: u8,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Message {
    Game(GameStatus),
    Robot(RobotStatus),
    PowerHeat(PowerHeat),
    Buff(Buff),
}
impl From<GameStatus> for Message {
    fn from(value: GameStatus) -> Self {
        Self::Game(value)
    }
}
impl From<RobotStatus> for Message {
    fn from(value: RobotStatus) -> Self {
        Self::Robot(value)
    }
}
impl From<PowerHeat> for Message {
    fn from(value: PowerHeat) -> Self {
        Self::PowerHeat(value)
    }
}
impl From<Buff> for Message {
    fn from(value: Buff) -> Self {
        Self::Buff(value)
    }
}
impl Message {
    pub fn payload(self) -> Result<(u16, Vec<u8>)> {
        let mut bytes = Vec::new();
        let command = match self {
            Self::Game(game) => {
                if !(1..=5).contains(&game.game_type) || game.game_progress > 5 {
                    return Err(admission("game_type requires 1..5 and game_progress 0..5"));
                }
                bytes.push(game.game_type | game.game_progress << 4);
                bytes.extend_from_slice(&game.stage_remain_time.to_le_bytes());
                bytes.extend_from_slice(&game.sync_timestamp.to_le_bytes());
                0x0001
            }
            Self::Robot(robot) => {
                if robot.power_outputs > 7 {
                    return Err(admission("robot power_outputs requires 0..7"));
                }
                bytes.extend_from_slice(&[robot.robot_id, robot.robot_level]);
                for value in [
                    robot.current_hp,
                    robot.maximum_hp,
                    robot.cooling_per_s,
                    robot.heat_limit,
                    robot.chassis_power_limit,
                ] {
                    bytes.extend_from_slice(&value.to_le_bytes());
                }
                bytes.push(robot.power_outputs);
                0x0201
            }
            Self::PowerHeat(power) => {
                bytes.extend_from_slice(&[0; 8]);
                for value in [power.buffer_energy_j, power.heat_17mm, power.heat_42mm] {
                    bytes.extend_from_slice(&value.to_le_bytes());
                }
                0x0202
            }
            Self::Buff(buff) => {
                bytes.push(buff.recovery_percent);
                bytes.extend_from_slice(&buff.cooling_per_s.to_le_bytes());
                bytes.extend_from_slice(&[buff.defence_percent, buff.vulnerability_percent]);
                bytes.extend_from_slice(&buff.attack_percent.to_le_bytes());
                bytes.push(buff.remaining_energy);
                0x0204
            }
        };
        Ok((command, bytes))
    }
    pub fn validate(self) -> Result<()> {
        self.payload().map(|_| ())
    }
}
pub fn encode_message(message: impl Into<Message>, sequence: u8) -> Result<Vec<u8>> {
    let (command, payload) = message.into().payload()?;
    encode_frame(command, &payload, sequence)
}
