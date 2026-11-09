//! Pure referee-domain schema; syntax and scalar typing remain shared YAML owners.

use super::{
    engine::{validate_referee_events, MAX_SCRIPT_EVENTS},
    factory::RefereeOptions,
    Buff, GameStatus, Hit, MatchDurations, PowerHeat, RefereeConfiguration, RefereeEvent,
    RefereeState, RobotStatus, ScriptEvent, Shot,
};
use crate::config::{
    assertions::{member, required, unsigned},
    yaml::{fields, parse_time, ManifestError, Mapping, Value},
};
use std::sync::Arc;
type Parsed<T> = std::result::Result<T, ManifestError>;
fn error(value: impl std::fmt::Display) -> ManifestError {
    ManifestError::new(value.to_string())
}
fn lower(error: ManifestError) -> sim_core::Failure {
    Arc::new(error)
}
macro_rules! record {
    ($function:ident,$record:ident,{$($field:ident:$ty:ty),+ $(,)?})=>{
        fn $function(value:&Value,label:&str)->Parsed<$record> {
            let names=&[$(stringify!($field)),+];
            let values=fields(value,names,names,label)?;
            Ok($record {$($field:unsigned(required(values,stringify!($field)),<$ty>::MAX as u64,&format!("{label}.{}",stringify!($field)))? as $ty),+})
        }
    }
}
record!(game,GameStatus,{game_type:u8,game_progress:u8,stage_remain_time:u16,sync_timestamp:u64});
record!(robot,RobotStatus,{robot_id:u8,robot_level:u8,current_hp:u16,maximum_hp:u16,cooling_per_s:u16,heat_limit:u16,chassis_power_limit:u16,power_outputs:u8});
record!(power,PowerHeat,{buffer_energy_j:u16,heat_17mm:u16,heat_42mm:u16});
record!(buff,Buff,{recovery_percent:u8,cooling_per_s:u16,defence_percent:u8,vulnerability_percent:u8,attack_percent:u16,remaining_energy:u8});
record!(durations,MatchDurations,{countdown_s:u16,battle_s:u16});
record!(hit,Hit,{damage_hp:u16});
record!(shot,Shot,{heat_17mm:u16,heat_42mm:u16});

fn events(value: &Value, label: &str) -> Parsed<Vec<ScriptEvent>> {
    let values = value
        .as_sequence()
        .filter(|values| values.len() <= MAX_SCRIPT_EVENTS)
        .ok_or_else(|| {
            error(format!(
                "{label} must contain at most {MAX_SCRIPT_EVENTS} entries"
            ))
        })?;
    let mut result = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        let label = format!("{label}[{index}]");
        let values = fields(
            value,
            &[
                "offset_ns",
                "hit",
                "shot",
                "game",
                "robot",
                "power_heat",
                "buff",
            ],
            &["offset_ns"],
            &label,
        )?;
        let selected: Vec<_> = ["hit", "shot", "game", "robot", "power_heat", "buff"]
            .into_iter()
            .filter(|kind| member(values, kind).is_some())
            .collect();
        if selected.len() != 1 {
            return Err(error(format!(
                "{label} must contain exactly one referee event value"
            )));
        }
        let kind = selected[0];
        let payload = required(values, kind);
        let where_ = format!("{label}.{kind}");
        let value = match kind {
            "hit" => RefereeEvent::Hit(hit(payload, &where_)?),
            "shot" => RefereeEvent::Shot(shot(payload, &where_)?),
            "game" => RefereeEvent::Game(game(payload, &where_)?),
            "robot" => RefereeEvent::Robot(robot(payload, &where_)?),
            "power_heat" => RefereeEvent::PowerHeat(power(payload, &where_)?),
            _ => RefereeEvent::Buff(buff(payload, &where_)?),
        };
        result.push(ScriptEvent {
            offset_ns: unsigned(
                required(values, "offset_ns"),
                u64::MAX,
                &format!("{label}.offset_ns"),
            )?,
            value,
        });
    }
    validate_referee_events(&result).map_err(error)?;
    Ok(result)
}
pub fn parse_events(value: &Value, label: &str) -> sim_core::Result<Vec<ScriptEvent>> {
    events(value, label).map_err(lower)
}
fn configuration(values: &Mapping, label: &str) -> Parsed<RefereeConfiguration> {
    let where_ = format!("{label}.initial");
    let groups = &["game", "robot", "power_heat", "buff"];
    let initial = fields(required(values, "initial"), groups, groups, &where_)?;
    let configuration = RefereeConfiguration {
        initial: RefereeState {
            game: game(required(initial, "game"), &format!("{where_}.game"))?,
            robot: robot(required(initial, "robot"), &format!("{where_}.robot"))?,
            power_heat: power(
                required(initial, "power_heat"),
                &format!("{where_}.power_heat"),
            )?,
            buff: buff(required(initial, "buff"), &format!("{where_}.buff"))?,
        },
        durations: durations(required(values, "durations"), &format!("{label}.durations"))?,
        events: member(values, "events")
            .map(|value| events(value, &format!("{label}.events")))
            .transpose()?
            .unwrap_or_default(),
        cooling_42mm_per_s: member(values, "cooling_42mm_per_s")
            .map(|value| {
                unsigned(
                    value,
                    u16::MAX as u64,
                    &format!("{label}.cooling_42mm_per_s"),
                )
            })
            .transpose()?
            .unwrap_or(0) as u16,
    };
    configuration.validate().map_err(error)?;
    Ok(configuration)
}
pub fn parse_configuration(value: &Value, label: &str) -> sim_core::Result<RefereeConfiguration> {
    let values = fields(
        value,
        &["initial", "durations", "events", "cooling_42mm_per_s"],
        &["initial", "durations"],
        label,
    )
    .map_err(lower)?;
    configuration(values, label).map_err(lower)
}
pub fn parse_options(value: &Value, label: &str) -> sim_core::Result<RefereeOptions> {
    let values = fields(
        value,
        &[
            "initial",
            "durations",
            "events",
            "cooling_42mm_per_s",
            "period",
        ],
        &["initial", "durations"],
        label,
    )
    .map_err(lower)?;
    let period_ns = member(values, "period")
        .map(|value| parse_time(value, &format!("{label}.period"), false))
        .transpose()
        .map_err(lower)?
        .unwrap_or(100_000_000);
    let options = RefereeOptions {
        configuration: configuration(values, label).map_err(lower)?,
        period_ns,
    };
    options.validate()?;
    Ok(options)
}
