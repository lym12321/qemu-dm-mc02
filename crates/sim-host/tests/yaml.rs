#![cfg(feature = "yaml")]

use sim_host::config::yaml::{
    fields, identifier, load_document, parse_document, parse_time, text_bytes, Value,
    MAX_FILE_BYTES, MAX_TIME_NS,
};

fn parse(text: &str) -> Value {
    parse_document(text.as_bytes()).unwrap()
}

fn string(text: &str) -> Value {
    Value::String(text.into())
}

fn integer(value: i64) -> Value {
    Value::Integer(value.into())
}

#[test]
fn scalar_values_follow_pinned_pyyaml_literals() {
    // Expected values are independently observed PyYAML 6.0.3 constructors,
    // not round-trips through a Rust serializer or YAML 1.2 DOM.
    assert_eq!(
        parse(
            r#"[yes, ON, off, true, FALSE, y, tRuE, 012, 0o12, 0x10,
                1_000, 12:34, 1:02:03.5, 1e3, 1.0e+3, .5e2, .5e+2,
                null, ~, "yes", !!str ON, ! "yes", !!bool tRuE,
                !!int "0o12", !!int "١٢", !!float "١.٢٥", !!null ignored,
                "2026-11-09", "A\0B"]"#
        ),
        Value::Sequence(vec![
            Value::Bool(true),
            Value::Bool(true),
            Value::Bool(false),
            Value::Bool(true),
            Value::Bool(false),
            string("y"),
            string("tRuE"),
            integer(10),
            string("0o12"),
            integer(16),
            integer(1000),
            integer(754),
            Value::Float(3723.5),
            string("1e3"),
            Value::Float(1000.0),
            string(".5e2"),
            Value::Float(50.0),
            Value::Null,
            Value::Null,
            string("yes"),
            string("ON"),
            Value::Bool(true),
            Value::Bool(true),
            integer(10),
            integer(12),
            Value::Float(1.25),
            Value::Null,
            string("2026-11-09"),
            string("A\0B"),
        ])
    );
    assert_eq!(
        parse("%YAML 1.2\n---\nx: yes").get("x"),
        Some(&Value::Bool(true))
    );
    assert_eq!(
        parse(
            "!!int 115792089237316195423570985008687907853269984665640564039457584007913129639936"
        )
        .as_integer()
        .unwrap()
        .to_string(),
        "115792089237316195423570985008687907853269984665640564039457584007913129639936"
    );
    assert_eq!(parse("-.inf"), Value::Float(f64::NEG_INFINITY));
    assert!(parse(".NaN").as_f64().unwrap().is_nan());
    assert_eq!(parse(""), Value::Null);
    assert_eq!(parse("---\n..."), Value::Null);
}

#[test]
fn binary_keeps_python_noise_padding_and_raw_nul() {
    for (text, expected) in [
        ("QQBC", b"A\0B".as_slice()),
        ("S G!k=", b"Hi"),
        ("SGk=====junk", b"Hi"),
        ("====QQ==", b"A"),
        ("Q=Q==", b"A"),
        ("QQ=A=", b"A\0"),
        ("QQ= =garbage", b"A"),
        ("Zh==", b"f"),
        ("====", b""),
    ] {
        assert_eq!(
            parse(&format!("!!binary '{text}'")),
            Value::Binary(expected.to_vec())
        );
    }
    for text in ["SGk", "QQ=", "A===", "SGk=é"] {
        assert!(
            parse_document(format!("!!binary '{text}'").as_bytes()).is_err(),
            "{text}"
        );
    }
}

#[test]
fn mapping_preserves_decoded_key_order_and_unique_string_keys() {
    assert_eq!(
        parse("!!map {z: !!seq [1, !!str 2], a: {}, 'yes': 3}"),
        Value::Mapping(vec![
            ("z".into(), Value::Sequence(vec![integer(1), string("2")])),
            ("a".into(), Value::Mapping(vec![])),
            ("yes".into(), integer(3)),
        ])
    );
    for (text, message) in [
        ("{a: 1, \"\\u0061\": 2}", "duplicate YAML key: a"),
        ("{true: value}", "mapping keys must be strings"),
        ("? [a, b]\n: value", "mapping keys must be strings"),
        ("x: &scalar 1", "anchors and aliases"),
        ("x: &sequence []", "anchors and aliases"),
        ("x: &mapping {}", "anchors and aliases"),
        ("x: *missing", "unknown anchor"),
        ("---\nx: 1\n---\ny: 2", "single YAML document"),
    ] {
        let error = parse_document(text.as_bytes()).unwrap_err();
        assert!(error.to_string().contains(message), "{text}: {error}");
    }
}

#[test]
fn unsupported_tags_and_invalid_constructors_never_become_strings() {
    for text in [
        "x: !custom hello",
        "x: !!python/object/apply:os.system [echo unsafe]",
        "x: {<<: {a: 1}}",
        "x: =",
        "x: !!bool invalid",
        "x: !!int 09",
        "x: !!int ''",
        "x: !!float invalid",
        "x: !!null [ignored]",
        "x: !!str [a]",
        "x: !!map [a]",
        "x: 2026-11-09",
        "x: !!timestamp '2026-11-09'",
        "x: !!set {a: null}",
        "x: !!omap [{a: 1}]",
        "x: !!pairs [{a: 1}]",
    ] {
        assert!(parse_document(text.as_bytes()).is_err(), "{text}");
    }
}

#[test]
fn composition_enforces_exact_depth_and_node_budgets() {
    let depth16 = format!("{}0{}", "[".repeat(15), "]".repeat(15));
    assert!(parse_document(depth16.as_bytes()).is_ok());
    let depth17 = format!("[{depth16}]");
    assert!(parse_document(depth17.as_bytes())
        .unwrap_err()
        .to_string()
        .contains("nesting"));
    let limit = format!("[{}]", vec!["0"; 39_999].join(","));
    assert_eq!(parse(&limit).as_sequence().unwrap().len(), 39_999);
    let excess = limit.replacen(']', ",0]", 1);
    assert!(parse_document(excess.as_bytes())
        .unwrap_err()
        .to_string()
        .contains("node limit"));
    // Count both key and value nodes, not just collection elements.
    let mapping = (0..20_000)
        .map(|index| format!("k{index}: 0"))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(parse_document(mapping.as_bytes())
        .unwrap_err()
        .to_string()
        .contains("node limit"));
}

#[test]
fn file_admission_is_bounded_utf8_and_regular_file_only() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("scene.yaml");
    let mut raw = b"x: 1\n#".to_vec();
    raw.resize(MAX_FILE_BYTES, b'x');
    std::fs::write(&path, &raw).unwrap();
    assert_eq!(
        load_document(&path).unwrap(),
        (path.canonicalize().unwrap(), parse("x: 1"))
    );
    raw.push(b'x');
    std::fs::write(&path, &raw).unwrap();
    assert!(load_document(&path)
        .unwrap_err()
        .to_string()
        .contains("1 MiB"));
    assert!(load_document(directory.path()).is_err());
    assert!(load_document(directory.path().join("missing")).is_err());
    for raw in [
        b"\xff".as_slice(),
        b"x: 1\0hidden: true",
        b"x: '\x7f'",
        b"x: \"\\uD800\"",
    ] {
        std::fs::write(&path, raw).unwrap();
        assert!(load_document(&path).is_err(), "{raw:?}");
    }
    #[cfg(unix)]
    {
        let socket = directory.path().join("socket");
        let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        assert!(load_document(&socket)
            .unwrap_err()
            .to_string()
            .contains("regular file"));
        let link = directory.path().join("manifest-link");
        std::fs::write(&path, b"x: 1").unwrap();
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert_eq!(
            load_document(&link).unwrap().0,
            path.canonicalize().unwrap()
        );
    }
}

#[test]
fn shared_helpers_preserve_schema_types_units_and_utf8_bytes() {
    let value = parse("{duration: 3600s, marker: \"A\\0B\", name: _node-2}");
    assert_eq!(
        fields(
            &value,
            &["duration", "marker", "name"],
            &["duration"],
            "runtime"
        )
        .unwrap()
        .len(),
        3
    );
    assert!(fields(&value, &["duration"], &[], "runtime").is_err());
    assert!(fields(
        &value,
        &["duration", "marker", "name"],
        &["absent"],
        "runtime"
    )
    .is_err());
    assert_eq!(
        parse_time(value.get("duration").unwrap(), "duration", false).unwrap(),
        MAX_TIME_NS
    );
    for (input, expected) in [
        ("1ns", 1),
        ("1us", 1000),
        ("1ms", 1_000_000),
        ("1s", 1_000_000_000),
    ] {
        assert_eq!(parse_time(&string(input), "time", true).unwrap(), expected);
    }
    for input in [
        Value::Bool(true),
        integer(1),
        string("1.5ms"),
        string("1e3ms"),
        string("-1ms"),
        string("3601s"),
        string("9999999999999s"),
    ] {
        assert!(parse_time(&input, "time", true).is_err(), "{input:?}");
    }
    assert_eq!(parse_time(&string("0s"), "time", true).unwrap(), 0);
    assert!(parse_time(&string("0s"), "time", false).is_err());
    assert_eq!(
        text_bytes(value.get("marker").unwrap(), "marker", false, 3).unwrap(),
        b"A\0B"
    );
    assert!(text_bytes(&string("é"), "marker", false, 1).is_err());
    assert_eq!(
        identifier(value.get("name").unwrap(), "name").unwrap(),
        "_node-2"
    );
    for input in ["1bad", "a.b", "é", "nul\0name", ""] {
        assert!(identifier(&string(input), "name").is_err());
    }
}
