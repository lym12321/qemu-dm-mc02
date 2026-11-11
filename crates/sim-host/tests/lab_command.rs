#![cfg(all(target_os = "linux", feature = "lab"))]
use sim_host::lab::{
    channels::AdcValue,
    command::{decode_uart, parse, split_words, Command, Lines, UartReceiveFormat},
    BoardCommand, LabInput,
};

#[test]
fn quote_and_escape_grammar_keeps_independent_cpython_words() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/lab_shlex.json")).unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let actual = split_words(case["input"].as_str().unwrap()).ok();
        let expected: Option<Vec<String>> = serde_json::from_value(case["words"].clone()).unwrap();
        assert_eq!(actual, expected, "{}", case["input"]);
    }
}

#[test]
fn serial_assistant_decodes_raw_hex_and_applies_only_selected_line_ending() {
    assert_eq!(
        decode_uart("hex", "crlf", "00 ff E4 b8 ad").unwrap(),
        b"\0\xff\xe4\xb8\xad\r\n"
    );
    assert_eq!(
        decode_uart("text", "none", "中\0").unwrap(),
        "中\0".as_bytes()
    );
    let ports = vec!["uart5".into()];
    let command = parse(br#"uart-send uart5 hex none "00 ff""#, &ports, &[], false).unwrap();
    assert!(
        matches!(command.board_command(), Some(BoardCommand::Input(LabInput::Uart { endpoint, data }))
        if endpoint == "uart5" && data == [0, 255])
    );
    assert!(matches!(
        parse(b"receive-format text", &ports, &[], false).unwrap(),
        Command::ReceiveFormat(UartReceiveFormat::Text)
    ));
    assert!(
        matches!(parse(b"action 'USER low'", &ports, &[], false).unwrap(), Command::Action(name) if name == "USER low")
    );
    for (format, ending, payload) in [
        ("hex", "none", "a a"),
        ("text", "bad", "x"),
        ("utf8", "lf", "x"),
    ] {
        assert!(decode_uart(format, ending, payload).is_err());
    }
    assert!(decode_uart("text", "crlf", &"x".repeat(4095)).is_err());
}

#[test]
fn commands_validate_public_boundaries_and_keep_literal_uart_bytes() {
    let (uarts, cans) = (vec!["uart1".into()], vec!["can1".into()]);
    let parse = |text: &[u8]| parse(text, &uarts, &cans, true);
    let Command::Inject(LabInput::Uart { data, .. }) =
        parse(br#"uart uart1 "hello world $(never) #yes \$""#).unwrap()
    else {
        panic!()
    };
    assert_eq!(data, br"hello world $(never) #yes \$");
    let Command::Inject(LabInput::Can { frame, .. }) =
        parse(br#"can can1 0x123 0 2 "aa bb""#).unwrap()
    else {
        panic!()
    };
    assert_eq!((frame.can_id(), frame.data()), (0x123, &[0xaa, 0xbb][..]));
    assert!(matches!(
        parse(b"adc ch19 voltage_uv 1_650_000").unwrap(),
        Command::Inject(LabInput::Adc {
            channel: 19,
            value: AdcValue::Voltage(1_650_000)
        })
    ));
    for invalid in [
        b"reset".as_slice(),
        b"gpio I0 1",
        b"gpio A0 2",
        b"vin -1",
        b"vin 01",
        b"uart uart2 x",
        b"can can1 1 0 9 aa",
        b"can can1 1 8 0 ''",
        b"can can1 1 0 2 'a a'",
        b"adc ch01 raw 1",
        b"adc ch32 raw 1",
        b"adc ch1 raw 65536",
        b"state extra",
        b"\xff",
    ] {
        assert!(parse(invalid).is_err(), "{invalid:?}");
    }
    assert!(sim_host::lab::command::parse(b"adc ch0 raw 1", &uarts, &cans, false).is_err());
}

#[test]
fn pipe_lines_preserve_partials_eof_and_recover_after_one_overlong_error() {
    let mut lines = Lines::default();
    lines.feed(b"gpio A");
    assert!(lines.next_line().is_none());
    lines.feed(b"0 1\nquit\n");
    assert_eq!(lines.next_line().unwrap().unwrap(), b"gpio A0 1");
    assert!(!lines.needs_read());
    assert_eq!(lines.next_line().unwrap().unwrap(), b"quit");
    lines.feed(&[b'x'; 4096]);
    assert!(lines.next_line().is_none());
    lines.feed(b"x");
    assert!(lines.next_line().unwrap().is_err());
    lines.feed(b"discarded suffix\nkey press");
    assert!(lines.next_line().is_none());
    lines.feed(b"");
    assert_eq!(lines.next_line().unwrap().unwrap(), b"key press");
    assert!(lines.finished());
    let mut lines = Lines::default();
    lines.feed(&[b'x'; 4096]);
    lines.feed(b"\nquit\n");
    assert_eq!(lines.next_line().unwrap().unwrap().len(), 4096);
    assert_eq!(lines.next_line().unwrap().unwrap(), b"quit");
}
