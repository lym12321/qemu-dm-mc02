# DM-MC02 binary co-simulation protocol

The protocol is deliberately fixed-width and little-endian so a MuJoCo or
Gazebo adapter can exchange samples without parsing JSON in the step loop.
Every frame has a 28-byte header:

```text
magic:u32 version:u16 type:u16 payload_len:u32 sequence:u64 virtual_time_ns:u64
```

The codec validates the magic, version, known type, exact payload length,
strictly increasing sequence/time, and finite IMU values. The IMU wire payload
is explicitly 24 bytes (six `float32` values), the telemetry wire payload is
12 bytes, and an ADC input payload is 8 bytes
(`channel:u16, raw:u16, reserved:u32`). ADC channels are limited to 0..31 and
ADC `reserved` must be zero. Analog ADC voltage frames use type 6 and are 12
bytes (`channel:u16, flags:u16, voltage_uv:u32, reserved:u32`), with voltage
limited to 0..3,300,000 uV. Telemetry `reserved` must also be zero. A RESET frame is
accepted as a session start and records its own sequence/time; the next normal
frame may start a new monotonic sequence.
The codec has no socket or thread dependency; `dm_mc02_transport` layers
Unix-domain and TCP stream transport on it without changing the deterministic
in-process path. Each transport frame is prefixed with a 4-byte little-endian
encoded frame length and is bounded before any payload read.

When a machine reset occurs while the QEMU chardev is open, QEMU emits an
outbound RESET marker before the forced telemetry snapshot. The new telemetry
session starts at sequence 1 with a strictly later virtual timestamp, so a
host can reset its validator without weakening monotonicity checks.
