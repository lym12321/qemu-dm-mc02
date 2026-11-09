#![cfg(feature = "referee")]
use sim_core::{Session, StepContext};
use sim_host::devices::referee::*;
use std::sync::Arc;
const NS: u64 = 1_000_000_000;
fn config() -> RefereeConfiguration {
    RefereeConfiguration {
        initial: RefereeState {
            game: GameStatus {
                game_type: 1,
                game_progress: 0,
                stage_remain_time: 0,
                sync_timestamp: 0x0102030405060708,
            },
            robot: RobotStatus {
                robot_id: 3,
                robot_level: 2,
                current_hp: 100,
                maximum_hp: 200,
                cooling_per_s: 3,
                heat_limit: 100,
                chassis_power_limit: 80,
                power_outputs: 7,
            },
            power_heat: PowerHeat {
                buffer_energy_j: 40,
                heat_17mm: 10,
                heat_42mm: 20,
            },
            buff: Buff {
                recovery_percent: 0,
                cooling_per_s: 0,
                defence_percent: 0,
                vulnerability_percent: 0,
                attack_percent: 0,
                remaining_energy: 0x80,
            },
        },
        durations: MatchDurations {
            countdown_s: 3,
            battle_s: 4,
        },
        events: vec![],
        cooling_42mm_per_s: 2,
    }
}
fn event(offset_ns: u64, value: RefereeEvent) -> ScriptEvent {
    ScriptEvent { offset_ns, value }
}
fn advance(engine: &mut RefereeEngine, target: u64, id: u64) {
    engine
        .step(StepContext::new(7, id, target, target - engine.t_sim_ns()).unwrap())
        .unwrap();
}
#[test]
fn heat_uses_integer_fractions_and_explicit_inputs_in_stable_order() {
    let mut configuration = config();
    configuration.events = vec![
        event(
            NS / 2,
            RefereeEvent::Shot(Shot {
                heat_17mm: 5,
                heat_42mm: 7,
            }),
        ),
        event(NS / 2, RefereeEvent::Hit(Hit { damage_hp: 30 })),
    ];
    let mut whole = RefereeEngine::new(configuration.clone()).unwrap();
    let mut split = RefereeEngine::new(configuration).unwrap();
    for engine in [&mut whole, &mut split] {
        engine.reset(Session::new(7, 0).unwrap()).unwrap();
    }
    advance(&mut whole, NS, 1);
    for (id, target) in [NS / 10, 3 * NS / 10, 8 * NS / 10, NS]
        .into_iter()
        .enumerate()
    {
        advance(&mut split, target, id as u64 + 1);
    }
    assert_eq!(whole.get_state().unwrap(), split.get_state().unwrap());
    let state = whole.get_state().unwrap();
    assert_eq!(
        (state.robot.current_hp, state.power_heat),
        (
            70,
            PowerHeat {
                buffer_energy_j: 40,
                heat_17mm: 12,
                heat_42mm: 25
            }
        )
    );
    assert_eq!(state.robot.cooling_per_s, 3);
}
#[test]
fn static_then_queued_ties_and_new_reset_origin_do_not_duplicate_inputs() {
    let mut configuration = config();
    let replacement = RobotStatus {
        current_hp: 50,
        ..configuration.initial.robot
    };
    configuration.events = vec![
        event(0, RefereeEvent::Hit(Hit { damage_hp: 20 })),
        event(0, RefereeEvent::Robot(replacement)),
        event(10, RefereeEvent::Hit(Hit { damage_hp: 5 })),
    ];
    let mut engine = RefereeEngine::new(configuration).unwrap();
    engine.reset(Session::new(7, 100).unwrap()).unwrap();
    assert_eq!(engine.get_state().unwrap().robot.current_hp, 50);
    engine
        .queue_events(
            &[
                event(10, RefereeEvent::Robot(replacement)),
                event(10, RefereeEvent::Hit(Hit { damage_hp: 10 })),
            ],
            7,
        )
        .unwrap();
    advance(&mut engine, 110, 1);
    assert_eq!(engine.get_state().unwrap().robot.current_hp, 40);
    engine.reset(Session::new(8, 1000).unwrap()).unwrap();
    assert_eq!(engine.get_state().unwrap().robot.current_hp, 50);
    engine
        .step(StepContext::new(8, 1, 1010, 10).unwrap())
        .unwrap();
    assert_eq!(engine.get_state().unwrap().robot.current_hp, 45);
}
#[test]
fn phase_expiry_uses_absolute_deadlines_ceil_and_bounded_catchup() {
    let mut configuration = config();
    configuration.initial.game.game_progress = 2;
    configuration.initial.game.stage_remain_time = 2;
    let mut engine = RefereeEngine::new(configuration.clone()).unwrap();
    let origin = 50 * NS;
    engine.reset(Session::new(7, origin).unwrap()).unwrap();
    for (i, (offset, phase, seconds)) in [
        (1, 2, 2),
        (NS, 2, 1),
        (2 * NS - 1, 2, 1),
        (2 * NS, 3, 3),
        (5 * NS, 4, 4),
        (9 * NS, 5, 0),
    ]
    .into_iter()
    .enumerate()
    {
        advance(&mut engine, origin + offset, i as u64 + 1);
        let game = engine.get_state().unwrap().game;
        assert_eq!(
            (
                game.game_progress,
                game.stage_remain_time,
                game.sync_timestamp
            ),
            (phase, seconds, 0x0102030405060708)
        );
    }
    let explicit = GameStatus {
        game_progress: 0,
        stage_remain_time: 9,
        ..configuration.initial.game
    };
    configuration.events = vec![event(2 * NS, RefereeEvent::Game(explicit))];
    let mut engine = RefereeEngine::new(configuration).unwrap();
    engine.reset(Session::new(7, 0).unwrap()).unwrap();
    advance(&mut engine, 20 * NS, 1);
    assert_eq!(engine.get_state().unwrap().game, explicit);
}
#[test]
fn overflow_freezes_original_error_without_publishing_earlier_partial_changes() {
    let mut configuration = config();
    configuration.initial.robot.cooling_per_s = 1;
    configuration.initial.power_heat.heat_17mm = u16::MAX;
    configuration.events = vec![
        event(NS / 4, RefereeEvent::Hit(Hit { damage_hp: 30 })),
        event(
            NS / 2,
            RefereeEvent::Shot(Shot {
                heat_17mm: 1,
                heat_42mm: 0,
            }),
        ),
    ];
    let initial = configuration.initial;
    let mut engine = RefereeEngine::new(configuration).unwrap();
    engine.reset(Session::new(7, 0).unwrap()).unwrap();
    let failure = engine
        .step(StepContext::new(7, 1, NS, NS).unwrap())
        .unwrap_err();
    assert_eq!(
        (engine.get_state().unwrap(), engine.t_sim_ns()),
        (initial, 0)
    );
    assert!(Arc::ptr_eq(
        &failure,
        &engine
            .step(StepContext::new(7, 1, 1, 1).unwrap())
            .unwrap_err()
    ));
    engine.reset(Session::new(8, 1000).unwrap()).unwrap();
    engine.close();
    assert_eq!(engine.get_state().unwrap(), initial);
}
#[test]
fn configuration_queue_and_context_rejections_are_pure_and_retryable() {
    let mut configuration = config();
    configuration.initial.robot.current_hp = 201;
    assert!(configuration.validate().is_err());
    let mut configuration = config();
    configuration.events = vec![event(1, RefereeEvent::Hit(Hit { damage_hp: 0 })); 1025];
    assert!(configuration.validate().is_err());
    let mut engine = RefereeEngine::new(config()).unwrap();
    engine.reset(Session::new(7, 100).unwrap()).unwrap();
    assert!(engine
        .step(StepContext::new(8, 1, 110, 10).unwrap())
        .is_err());
    advance(&mut engine, 110, 1);
    assert!(engine
        .queue_events(&[event(9, RefereeEvent::Hit(Hit { damage_hp: 1 }))], 7)
        .is_err());
    let batch = vec![event(10, RefereeEvent::Hit(Hit { damage_hp: 0 })); 1024];
    engine.queue_events(&batch, 7).unwrap();
    assert!(engine
        .queue_events(&[event(10, RefereeEvent::Hit(Hit { damage_hp: 1 }))], 7)
        .unwrap_err()
        .to_string()
        .contains("1024"));
    advance(&mut engine, 111, 2);
    engine
        .queue_events(
            &[event(u64::MAX, RefereeEvent::Hit(Hit { damage_hp: 1 }))],
            7,
        )
        .unwrap();
    advance(&mut engine, 112, 3);
    assert_eq!(engine.get_state().unwrap().robot.current_hp, 100);
}
