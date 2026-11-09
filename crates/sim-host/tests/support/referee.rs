use sim_host::devices::referee::*;
pub fn state() -> RefereeState {
    RefereeState {
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
    }
}
