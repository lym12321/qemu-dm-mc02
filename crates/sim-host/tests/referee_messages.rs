#![cfg(feature = "referee")]
use sim_host::devices::referee::{
    decode_frame, encode_message, Buff, GameStatus, Message, PowerHeat, RobotStatus,
};
fn hex(value: &str) -> Vec<u8> {
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|v| u8::from_str_radix(std::str::from_utf8(v).unwrap(), 16).unwrap())
        .collect()
}
// Official V1.1.0 20251217 PDF pp9/10/14–17, SHA256
// c08c66042b3c9129e3a176d343df54cf72538bb11902dde762a504890969d12f.
// These literal payloads come from the published layouts, not a packer.
#[test]
fn fixed_official_payloads_consume_the_validated_envelope() {
    let cases: [(Message, u16, &str); 4] = [
        (
            GameStatus {
                game_type: 4,
                game_progress: 3,
                stage_remain_time: 0xabcd,
                sync_timestamp: 0xf0e1d2c3b4a59687,
            }
            .into(),
            0x0001,
            "34cdab8796a5b4c3d2e1f0",
        ),
        (
            RobotStatus {
                robot_id: 0x65,
                robot_level: 0xe7,
                current_hp: 0xffee,
                maximum_hp: 0x0110,
                cooling_per_s: 0x2345,
                heat_limit: 0x6789,
                chassis_power_limit: 0xabcd,
                power_outputs: 5,
            }
            .into(),
            0x0201,
            "65e7eeff100145238967cdab05",
        ),
        (
            PowerHeat {
                buffer_energy_j: 0x1234,
                heat_17mm: 0xabcd,
                heat_42mm: 0xfedc,
            }
            .into(),
            0x0202,
            "00000000000000003412cdabdcfe",
        ),
        (
            Buff {
                recovery_percent: 0xe1,
                cooling_per_s: 0x2345,
                defence_percent: 0x67,
                vulnerability_percent: 0x89,
                attack_percent: 0xabcd,
                remaining_energy: 0xfe,
            }
            .into(),
            0x0204,
            "e145236789cdabfe",
        ),
    ];
    for (message, command, payload) in cases {
        let frame = decode_frame(&encode_message(message, 0xa5).unwrap()).unwrap();
        assert_eq!(
            (frame.command_id, frame.sequence, frame.payload),
            (command, 0xa5, hex(payload))
        );
    }
}
#[test]
fn constrained_wire_bits_reject_without_inventing_hp_or_rule_policy() {
    for (kind, phase) in [(0, 0), (6, 0), (1, 6)] {
        assert!(encode_message(
            GameStatus {
                game_type: kind,
                game_progress: phase,
                stage_remain_time: 0,
                sync_timestamp: 0
            },
            0
        )
        .is_err());
    }
    let robot = RobotStatus {
        robot_id: 255,
        robot_level: 255,
        current_hp: u16::MAX,
        maximum_hp: 0,
        cooling_per_s: u16::MAX,
        heat_limit: u16::MAX,
        chassis_power_limit: u16::MAX,
        power_outputs: 7,
    };
    assert_eq!(
        Message::Robot(robot).payload().unwrap().1,
        [vec![255; 4], vec![0; 2], vec![255; 6], vec![7]].concat()
    );
    assert!(encode_message(
        RobotStatus {
            power_outputs: 8,
            ..robot
        },
        0
    )
    .is_err());
    assert_eq!(
        Message::PowerHeat(PowerHeat {
            buffer_energy_j: u16::MAX,
            heat_17mm: u16::MAX,
            heat_42mm: u16::MAX
        })
        .payload()
        .unwrap()
        .1,
        [vec![0; 8], vec![255; 6]].concat()
    );
}
