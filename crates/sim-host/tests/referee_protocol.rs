#![cfg(feature = "referee")]
use sim_host::devices::referee::protocol::{
    decode_frame, encode_frame, Frame, MAX_FRAME_BYTES, MAX_PAYLOAD_BYTES,
};
fn hex(value: &str) -> Vec<u8> {
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|v| u8::from_str_radix(std::str::from_utf8(v).unwrap(), 16).unwrap())
        .collect()
}
// Independent trobot utils/src/crc.cc table literals, sha256
// 6b5651ba4be79e3ab8f27c9755f3bbbad988bc8d31ceb5dd2df9b706ef8e3530.
// Game vector also cross-checked against pinned DJI tables in original audit.
#[test]
fn independent_complete_frame_literals_and_maximum_profile() {
    for (command, payload, sequence, wire) in [
        (0x0000, "", 0x00, "a5000000c300004c83"),
        (0xffff, "", 0xff, "a50000fff6ffff3509"),
        (
            0x0001,
            "412c010000000000000000",
            0,
            "a50b0000020100412c0100000000000000009357",
        ),
        (
            0x0201,
            "03026400c8000a006400500007",
            1,
            "a50d00018d010203026400c8000a006400500007c4e8",
        ),
    ] {
        let (payload, wire) = (hex(payload), hex(wire));
        assert_eq!(encode_frame(command, &payload, sequence).unwrap(), wire);
        assert_eq!(
            decode_frame(&wire).unwrap(),
            Frame {
                command_id: command,
                payload,
                sequence
            }
        );
    }
    assert_eq!((MAX_PAYLOAD_BYTES, MAX_FRAME_BYTES), (119, 128));
    let payload: Vec<_> = (0..119).collect();
    let wire = [hex("a577005af33412"), payload.clone(), hex("29bb")].concat();
    assert_eq!(encode_frame(0x1234, &payload, 0x5a).unwrap(), wire);
    assert_eq!(decode_frame(&wire).unwrap().payload, payload);
    assert!(encode_frame(1, &[0; 120], 0).is_err());
}
#[test]
fn truncation_trailing_bytes_length_sof_and_both_crcs_reject() {
    let wire = hex("a50b0000020100412c0100000000000000009357");
    for n in [0, 4, 8, wire.len() - 1] {
        assert!(decode_frame(&wire[..n]).is_err());
    }
    let mut extended = wire.clone();
    extended.push(0);
    assert!(decode_frame(&extended).is_err());
    for (offset, error) in [
        (0, "SOF"),
        (1, "payload length"),
        (3, "CRC8"),
        (4, "CRC8"),
        (5, "CRC16"),
        (7, "CRC16"),
        (wire.len() - 1, "CRC16"),
    ] {
        let mut bad = wire.clone();
        bad[offset] ^= 1;
        assert!(decode_frame(&bad).unwrap_err().to_string().contains(error));
    }
    assert!(decode_frame(&[0; 129]).is_err());
}
