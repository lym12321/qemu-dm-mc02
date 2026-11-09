//! Explicit referee fixture, never competition adjudication or a world clock.
//!
//! HeatLimit heatCB/timerCB narrow BSD-3-Clause port from rm_control commit
//! c5205eb193a8cda6147efe760eafedd0a3ed0104 (Qiayuan Liao, 2021).
//! Changes: explicit increments, integer virtual ns, no ROS/mutex/punishment.
//! Phase kernel adapted from XDU-IRobot/rm_referee_ros2 MatchControlTiming,
//! Apache-2.0, commit45da10a5317d47cc4b5d5968ec553946c43ebe0d.
//! Changes: explicit durations/absolute deadlines, no Qt/widgets/V2 constants.
//! The adjacent original license files retain their notices.

use super::messages::{Buff, GameStatus, Message, PowerHeat, RobotStatus};
use sim_core::{Error, Failure, Result, Session, StepContext};
use std::{collections::VecDeque, sync::Arc};
const NS: u64 = 1_000_000_000;
const MAX_HEAT: u64 = 65535 * NS;
pub const MAX_SCRIPT_EVENTS: usize = 1024;
fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RefereeState {
    pub game: GameStatus,
    pub robot: RobotStatus,
    pub power_heat: PowerHeat,
    pub buff: Buff,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MatchDurations {
    pub countdown_s: u16,
    pub battle_s: u16,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Hit {
    pub damage_hp: u16,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Shot {
    pub heat_17mm: u16,
    pub heat_42mm: u16,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RefereeEvent {
    Hit(Hit),
    Shot(Shot),
    Game(GameStatus),
    Robot(RobotStatus),
    PowerHeat(PowerHeat),
    Buff(Buff),
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ScriptEvent {
    pub offset_ns: u64,
    pub value: RefereeEvent,
}
#[derive(Clone, Debug)]
pub struct RefereeConfiguration {
    pub initial: RefereeState,
    pub durations: MatchDurations,
    pub events: Vec<ScriptEvent>,
    pub cooling_42mm_per_s: u16,
}
fn admit_message(message: Message) -> Result<()> {
    message.validate()?;
    match message {
        Message::Robot(value) if value.current_hp > value.maximum_hp => {
            Err(admission("current_hp must not exceed maximum_hp"))
        }
        Message::Game(value)
            if matches!(value.game_progress, 2..=4) && value.stage_remain_time == 0 =>
        {
            Err(admission(
                "active game phase requires positive remaining seconds",
            ))
        }
        _ => Ok(()),
    }
}
pub fn validate_referee_events(events: &[ScriptEvent]) -> Result<()> {
    if events.len() > MAX_SCRIPT_EVENTS {
        return Err(admission("referee script exceeds 1024 events"));
    }
    if events
        .windows(2)
        .any(|pair| pair[0].offset_ns > pair[1].offset_ns)
    {
        return Err(admission("referee script offsets must be nondecreasing"));
    }
    for event in events {
        match event.value {
            RefereeEvent::Hit(_) | RefereeEvent::Shot(_) => {}
            RefereeEvent::Game(value) => admit_message(value.into())?,
            RefereeEvent::Robot(value) => admit_message(value.into())?,
            RefereeEvent::PowerHeat(value) => admit_message(value.into())?,
            RefereeEvent::Buff(value) => admit_message(value.into())?,
        }
    }
    Ok(())
}
impl RefereeConfiguration {
    pub fn validate(&self) -> Result<()> {
        for message in [
            self.initial.game.into(),
            self.initial.robot.into(),
            self.initial.power_heat.into(),
            self.initial.buff.into(),
        ] {
            admit_message(message)?;
        }
        if self.durations.countdown_s == 0 || self.durations.battle_s == 0 {
            return Err(admission("referee durations must be positive"));
        }
        validate_referee_events(&self.events)
    }
}
fn deadline(game: GameStatus, now: u64) -> Option<u128> {
    matches!(game.game_progress, 2..=4)
        .then_some(now as u128 + game.stage_remain_time as u128 * NS as u128)
}
#[derive(Clone, Copy)]
struct Completed {
    state: RefereeState,
    heat17: u64,
    heat42: u64,
    deadline: Option<u128>,
    cursor: usize,
}

/// Sole explicit-state owner. Only successful calculations publish snapshots;
/// dynamic events are bounded and retain static-before-queued order on ties.
/// Getters retain the last completed snapshot even after failure/close.
pub struct RefereeEngine {
    configuration: RefereeConfiguration,
    pending: VecDeque<ScriptEvent>,
    session: Option<Session>,
    time_ns: u64,
    step_id: u64,
    completed: Option<Completed>,
    failure: Option<Failure>,
    closed: bool,
}
impl RefereeEngine {
    pub fn new(configuration: RefereeConfiguration) -> Result<Self> {
        configuration.validate()?;
        Ok(Self {
            configuration,
            pending: VecDeque::new(),
            session: None,
            time_ns: 0,
            step_id: 0,
            completed: None,
            failure: None,
            closed: false,
        })
    }
    pub fn t_sim_ns(&self) -> u64 {
        self.time_ns
    }
    pub fn closed(&self) -> bool {
        self.closed
    }
    pub fn get_state(&self) -> Result<RefereeState> {
        self.completed
            .map(|value| value.state)
            .ok_or_else(|| state_error("referee has no completed reset"))
    }
    fn ready(&self) -> Result<Session> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.closed {
            return Err(state_error("referee is closed"));
        }
        self.session
            .ok_or_else(|| state_error("referee requires a successful reset"))
    }
    pub fn queue_events(&mut self, events: &[ScriptEvent], session_id: u64) -> Result<()> {
        let session = self.ready()?;
        if session_id != session.session_id {
            return Err(admission("referee events must match the active session"));
        }
        validate_referee_events(events)?;
        let minimum = self
            .pending
            .back()
            .map_or(self.time_ns - session.t_sim_ns, |event| event.offset_ns);
        if events
            .first()
            .is_some_and(|event| event.offset_ns < minimum)
        {
            return Err(admission(
                "referee events must follow completed time and pending order",
            ));
        }
        if self.pending.len() + events.len() > MAX_SCRIPT_EVENTS {
            return Err(Arc::new(Error::Capacity(
                "pending referee events exceed 1024".into(),
            )));
        }
        self.pending.extend(events.iter().copied());
        Ok(())
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        if self.closed {
            return Err(state_error("referee is closed"));
        }
        Session::new(session.session_id, session.t_sim_ns)?;
        let initial = self.configuration.initial;
        let runtime = Completed {
            state: initial,
            heat17: initial.power_heat.heat_17mm as u64 * NS,
            heat42: initial.power_heat.heat_42mm as u64 * NS,
            deadline: deadline(initial.game, session.t_sim_ns),
            cursor: 0,
        };
        let result = self.simulate(
            runtime,
            session.t_sim_ns,
            session.t_sim_ns,
            session.t_sim_ns,
            &VecDeque::new(),
        );
        match result {
            Ok((completed, _)) => {
                self.completed = Some(completed);
                self.session = Some(session);
                self.time_ns = session.t_sim_ns;
                self.step_id = 0;
                self.pending.clear();
                self.failure = None;
                Ok(())
            }
            Err(error) => {
                self.failure = Some(error.clone());
                Err(error)
            }
        }
    }
    pub fn step(&mut self, context: StepContext) -> Result<()> {
        let session = self.ready()?;
        StepContext::new(
            context.session_id,
            context.step_id,
            context.t_sim_ns,
            context.dt_ns,
        )?;
        if context.session_id != session.session_id
            || Some(context.step_id) != self.step_id.checked_add(1)
            || Some(context.t_sim_ns) != self.time_ns.checked_add(context.dt_ns)
        {
            return Err(admission(
                "referee step must follow its session and exact target",
            ));
        }
        match self.simulate(
            self.completed.expect("ready reset"),
            session.t_sim_ns,
            self.time_ns,
            context.t_sim_ns,
            &self.pending,
        ) {
            Ok((completed, consumed)) => {
                self.completed = Some(completed);
                self.pending.drain(..consumed);
                self.time_ns = context.t_sim_ns;
                self.step_id = context.step_id;
                Ok(())
            }
            Err(error) => {
                self.failure = Some(error.clone());
                Err(error)
            }
        }
    }
    fn phase_to(&self, game: &mut GameStatus, due: &mut Option<u128>, target: u64) {
        while due.is_some_and(|deadline| target as u128 >= deadline) {
            game.game_progress += 1;
            if game.game_progress == 5 {
                game.stage_remain_time = 0;
                *due = None;
            } else {
                let seconds = if game.game_progress == 3 {
                    self.configuration.durations.countdown_s
                } else {
                    self.configuration.durations.battle_s
                };
                game.stage_remain_time = seconds;
                *due = due.map(|deadline| deadline + seconds as u128 * NS as u128);
            }
        }
        if let Some(deadline) = due {
            game.stage_remain_time = ((*deadline - target as u128).div_ceil(NS as u128)) as u16;
        }
    }
    fn simulate(
        &self,
        mut runtime: Completed,
        origin: u64,
        start: u64,
        target: u64,
        pending: &VecDeque<ScriptEvent>,
    ) -> Result<(Completed, usize)> {
        let mut consumed = 0;
        let mut now = start;
        loop {
            let scripted = self.configuration.events.get(runtime.cursor);
            let queued = pending.get(consumed);
            let from_queue = queued.is_some_and(|queued| {
                scripted.is_none_or(|event| queued.offset_ns < event.offset_ns)
            });
            let event = if from_queue { queued } else { scripted };
            // Wider arithmetic keeps admitted distant offsets from wrapping into this session.
            let due = event.map(|event| origin as u128 + event.offset_ns as u128);
            let at = due
                .filter(|due| *due <= target as u128)
                .map_or(target, |due| due as u64);
            let elapsed = at - now;
            runtime.heat17 = runtime.heat17.saturating_sub(
                (runtime.state.robot.cooling_per_s as u128 * elapsed as u128).min(u64::MAX as u128)
                    as u64,
            );
            runtime.heat42 = runtime.heat42.saturating_sub(
                (self.configuration.cooling_42mm_per_s as u128 * elapsed as u128)
                    .min(u64::MAX as u128) as u64,
            );
            self.phase_to(&mut runtime.state.game, &mut runtime.deadline, at);
            now = at;
            if due.is_none_or(|due| due > target as u128) {
                break;
            }
            match event.expect("due event").value {
                RefereeEvent::Hit(hit) => {
                    runtime.state.robot.current_hp =
                        runtime.state.robot.current_hp.saturating_sub(hit.damage_hp)
                }
                RefereeEvent::Shot(shot) => {
                    runtime.heat17 += shot.heat_17mm as u64 * NS;
                    runtime.heat42 += shot.heat_42mm as u64 * NS;
                    if runtime.heat17 > MAX_HEAT || runtime.heat42 > MAX_HEAT {
                        return Err(state_error("referee heat exceeds the unsigned16 profile"));
                    }
                }
                RefereeEvent::Game(game) => {
                    runtime.state.game = game;
                    runtime.deadline = deadline(game, at);
                }
                RefereeEvent::Robot(robot) => runtime.state.robot = robot,
                RefereeEvent::PowerHeat(power) => {
                    runtime.state.power_heat = power;
                    runtime.heat17 = power.heat_17mm as u64 * NS;
                    runtime.heat42 = power.heat_42mm as u64 * NS;
                }
                RefereeEvent::Buff(buff) => runtime.state.buff = buff,
            }
            if from_queue {
                consumed += 1;
            } else {
                runtime.cursor += 1;
            }
        }
        runtime.state.power_heat.heat_17mm = (runtime.heat17 / NS) as u16;
        runtime.state.power_heat.heat_42mm = (runtime.heat42 / NS) as u16;
        Ok((runtime, consumed))
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.pending.clear();
    }
}
