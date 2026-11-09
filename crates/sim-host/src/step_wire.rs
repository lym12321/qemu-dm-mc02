//! Safe host values over the canonical portable C co-simulation codecs.
//!
//! Body codecs own no outer stream framing, session state, retries or clocks.
//! Native layout is defined by cosim/dm_mc02_{wire,v2_wire,v2_payload}.h.
use crate::{Error, Result};

pub const MAX_PAYLOAD: usize = 512;
pub const BODY_HEADER_SIZE: usize = 40;

fn invalid(message: &'static str) -> crate::Failure {
    Error::Admission(message).into()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u16)]
pub enum Kind {
    Reset = 1,
    Step = 2,
    StepAck = 3,
    Diagnostics = 4,
    ResetAck = 5,
    StepDone = 6,
    Telemetry = 7,
    MotorState = 8,
}

impl TryFrom<u16> for Kind {
    type Error = crate::Failure;
    fn try_from(value: u16) -> Result<Self> {
        match value {
            1 => Ok(Self::Reset),
            2 => Ok(Self::Step),
            3 => Ok(Self::StepAck),
            4 => Ok(Self::Diagnostics),
            5 => Ok(Self::ResetAck),
            6 => Ok(Self::StepDone),
            7 => Ok(Self::Telemetry),
            8 => Ok(Self::MotorState),
            _ => Err(invalid("unknown v2 frame kind")),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub kind: Kind,
    pub step_id: u64,
    pub time_ns: u64,
    pub dt_ns: u64,
    pub session_id: u32,
    pub payload: Vec<u8>,
}

#[repr(C)]
#[derive(Default)]
struct NativeHeader {
    version: u16,
    kind: u16,
    payload_len: u32,
    step_id: u64,
    time_ns: u64,
    dt_ns: u64,
    session_id: u32,
}
#[repr(C)]
struct NativeFrame {
    header: NativeHeader,
    payload: [u8; MAX_PAYLOAD],
}
impl Default for NativeFrame {
    fn default() -> Self {
        Self {
            header: NativeHeader::default(),
            payload: [0; MAX_PAYLOAD],
        }
    }
}
#[repr(C)]
#[derive(Default)]
struct NativeSection {
    kind: u16,
    flags: u16,
    length: u32,
    payload: *const u8,
}

unsafe extern "C" {
    fn dm_mc02_v2_wire_encode(out: *mut u8, capacity: usize, frame: *const NativeFrame) -> usize;
    fn dm_mc02_v2_wire_decode(frame: *mut NativeFrame, wire: *const u8, length: usize) -> bool;
    fn dm_mc02_v2_wire_step_payload_begin(
        payload: *const u8,
        length: usize,
        count: *mut u16,
        marker: *mut u16,
    ) -> bool;
    fn dm_mc02_v2_wire_next_section(
        payload: *const u8,
        length: usize,
        offset: *mut usize,
        section: *mut NativeSection,
    ) -> bool;
    fn dm_mc02_v2_wire_is_compact_imu(payload: *const u8, length: usize) -> bool;
}

impl Frame {
    /// Encode the body only; the transport owns its four-byte length prefix.
    pub fn encode_body(&self) -> Result<Vec<u8>> {
        if self.payload.len() > MAX_PAYLOAD {
            return Err(invalid("v2 payload exceeds 512 bytes"));
        }
        let mut native = NativeFrame {
            header: NativeHeader {
                version: 2,
                kind: self.kind as u16,
                payload_len: self.payload.len() as u32,
                step_id: self.step_id,
                time_ns: self.time_ns,
                dt_ns: self.dt_ns,
                session_id: self.session_id,
            },
            ..NativeFrame::default()
        };
        native.payload[..self.payload.len()].copy_from_slice(&self.payload);
        let mut output = vec![0; BODY_HEADER_SIZE + self.payload.len()];
        // SAFETY: repr(C) values match the headers; all buffers are live for
        // the call, and C checks the supplied output capacity before writing.
        let size = unsafe { dm_mc02_v2_wire_encode(output.as_mut_ptr(), output.len(), &native) };
        if size == 0 {
            return Err(invalid("invalid v2 frame"));
        }
        output.truncate(size);
        Ok(output)
    }

    pub fn decode_body(body: &[u8]) -> Result<Self> {
        let mut native = NativeFrame::default();
        // SAFETY: C bounds-checks the borrowed input before each read and writes
        // only the fixed-size initialized native output, without retaining pointers.
        if !unsafe { dm_mc02_v2_wire_decode(&mut native, body.as_ptr(), body.len()) } {
            return Err(invalid("invalid v2 frame body"));
        }
        Ok(Self {
            kind: Kind::try_from(native.header.kind)?,
            step_id: native.header.step_id,
            time_ns: native.header.time_ns,
            dt_ns: native.header.dt_ns,
            session_id: native.header.session_id,
            payload: native.payload[..native.header.payload_len as usize].to_vec(),
        })
    }
}

// Each wrapper uses the existing C validator/codec, including reserved fields,
// status and capability masks, exact lengths and STEP_DONE consumption masks.
macro_rules! payload {
    ($name:ident, $size:literal, $encode:ident, $decode:ident, {$($field:ident: $ty:ty),* $(,)?}) => {
        #[repr(C)]
        #[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
        pub struct $name { $(pub $field: $ty,)* }
        unsafe extern "C" {
            fn $encode(out: *mut u8, capacity: usize, value: *const $name) -> usize;
            fn $decode(value: *mut $name, payload: *const u8, length: usize) -> bool;
        }
        impl $name {
            pub fn encode(&self) -> Result<Vec<u8>> {
                let mut bytes = vec![0; $size];
                // SAFETY: C uses the matching repr(C) value and bounded output;
                // neither pointer escapes the call.
                let size = unsafe { $encode(bytes.as_mut_ptr(), bytes.len(), self) };
                if size == 0 { return Err(invalid(concat!("invalid ", stringify!($name)))); }
                bytes.truncate(size);
                Ok(bytes)
            }
            pub fn decode(bytes: &[u8]) -> Result<Self> {
                let mut value = Self::default();
                // SAFETY: C validates input length/ranges before populating the
                // matching repr(C) output and retains no borrowed pointers.
                if !unsafe { $decode(&mut value, bytes.as_ptr(), bytes.len()) } {
                    return Err(invalid(concat!("invalid ", stringify!($name))));
                }
                Ok(value)
            }
        }
    };
}
payload!(ResetAck, 24, dm_mc02_v2_payload_encode_reset_ack, dm_mc02_v2_payload_decode_reset_ack,
    {status: u32, capabilities: u32, virtual_time_ns: u64, queue_depth: u32});
payload!(StepAck, 16, dm_mc02_v2_payload_encode_step_ack, dm_mc02_v2_payload_decode_step_ack,
    {status: u32, queue_depth: u32, dropped_count: u32});
payload!(Diagnostics, 40, dm_mc02_v2_payload_encode_diagnostics, dm_mc02_v2_payload_decode_diagnostics,
    {rx_frames: u64, rx_bad_frames: u64, rx_dropped_bytes: u64, tx_frames: u64, tx_dropped: u64});
payload!(StepDone, 20, dm_mc02_v2_payload_encode_step_done, dm_mc02_v2_payload_decode_step_done,
    {status: u32, consumed_mask: u32, missing_mask: u32, queue_depth: u32, dropped_count: u32});
payload!(Telemetry, 24, dm_mc02_v2_payload_encode_board_telemetry, dm_mc02_v2_payload_decode_board_telemetry,
    {led_rgb: u32, led_brightness: u32, buzzer: u32, board_flags: u32, buzzer_frequency_hz: u32, buzzer_duty_permille: u32});
payload!(AdcInput, 8, dm_mc02_v2_payload_encode_adc_input, dm_mc02_v2_payload_decode_adc_input,
    {channel: u16, raw: u16});
payload!(AdcVoltage, 12, dm_mc02_v2_payload_encode_adc_voltage, dm_mc02_v2_payload_decode_adc_voltage,
    {channel: u16, flags: u16, voltage_uv: u32});

/// Compact IMU values keep native units and do not infer axes, gravity or time.
#[derive(Clone, Debug, PartialEq)]
pub struct ImuSample {
    pub gyro_dps: [f32; 3],
    pub accel_g: [f32; 3],
    pub gyro_bias_dps: [f32; 3],
    pub accel_bias_g: [f32; 3],
    pub sensor_time_ns: u64,
    pub sample_id: u32,
}
impl ImuSample {
    pub fn encode(&self) -> Result<Vec<u8>> {
        let mut bytes = Vec::with_capacity(60);
        for value in self
            .gyro_dps
            .iter()
            .chain(&self.accel_g)
            .chain(&self.gyro_bias_dps)
            .chain(&self.accel_bias_g)
        {
            if !value.is_finite() {
                return Err(invalid("IMU values must be finite binary32"));
            }
            bytes.extend(value.to_le_bytes());
        }
        bytes.extend(self.sensor_time_ns.to_le_bytes());
        bytes.extend(self.sample_id.to_le_bytes());
        Ok(bytes)
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        if bytes.len() != 60 {
            return Err(invalid("IMU sample must contain 60 bytes"));
        }
        let mut values = [0.0; 12];
        for (value, raw) in values.iter_mut().zip(bytes[..48].chunks_exact(4)) {
            *value = f32::from_le_bytes(raw.try_into().unwrap());
            if !value.is_finite() {
                return Err(invalid("IMU values must be finite binary32"));
            }
        }
        Ok(Self {
            gyro_dps: values[..3].try_into().unwrap(),
            accel_g: values[3..6].try_into().unwrap(),
            gyro_bias_dps: values[6..9].try_into().unwrap(),
            accel_bias_g: values[9..12].try_into().unwrap(),
            sensor_time_ns: u64::from_le_bytes(bytes[48..56].try_into().unwrap()),
            sample_id: u32::from_le_bytes(bytes[56..].try_into().unwrap()),
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MotorCommandItem {
    pub index: u16,
    pub command: f32,
}
#[derive(Clone, Debug, PartialEq)]
pub struct MotorCommand {
    pub flags: u16,
    pub commands: Vec<MotorCommandItem>,
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MotorStateItem {
    pub index: u16,
    pub position: f32,
    pub velocity: f32,
    pub effort: f32,
}
#[derive(Clone, Debug, PartialEq)]
pub struct MotorState {
    pub states: Vec<MotorStateItem>,
}

fn motor_indices(indices: impl Iterator<Item = u16>, count: usize) -> Result<()> {
    if count > u16::MAX as usize {
        return Err(invalid("too many motors"));
    }
    let mut seen = vec![false; count];
    for index in indices {
        let value = seen
            .get_mut(index as usize)
            .ok_or_else(|| invalid("motor index outside count"))?;
        if *value {
            return Err(invalid("duplicate motor index"));
        }
        *value = true;
    }
    Ok(())
}
impl MotorCommand {
    pub fn encode(&self) -> Result<Vec<u8>> {
        motor_indices(self.commands.iter().map(|v| v.index), self.commands.len())?;
        let mut bytes = Vec::with_capacity(4 + 6 * self.commands.len());
        bytes.extend((self.commands.len() as u16).to_le_bytes());
        bytes.extend(self.flags.to_le_bytes());
        for value in &self.commands {
            if !value.command.is_finite() {
                return Err(invalid("motor command must be finite"));
            }
            bytes.extend(value.index.to_le_bytes());
            bytes.extend(value.command.to_le_bytes());
        }
        Ok(bytes)
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        if bytes.len() < 4 {
            return Err(invalid("truncated motor command"));
        }
        let count = u16::from_le_bytes(bytes[..2].try_into().unwrap()) as usize;
        if bytes.len() != 4 + 6 * count {
            return Err(invalid("invalid motor command length"));
        }
        let mut commands = Vec::with_capacity(count);
        for value in bytes[4..].chunks_exact(6) {
            let command = f32::from_le_bytes(value[2..].try_into().unwrap());
            if !command.is_finite() {
                return Err(invalid("motor command must be finite"));
            }
            commands.push(MotorCommandItem {
                index: u16::from_le_bytes(value[..2].try_into().unwrap()),
                command,
            });
        }
        motor_indices(commands.iter().map(|v| v.index), count)?;
        Ok(Self {
            flags: u16::from_le_bytes(bytes[2..4].try_into().unwrap()),
            commands,
        })
    }
}
impl MotorState {
    pub fn encode(&self) -> Result<Vec<u8>> {
        motor_indices(self.states.iter().map(|v| v.index), self.states.len())?;
        let mut bytes = Vec::with_capacity(2 + 14 * self.states.len());
        bytes.extend((self.states.len() as u16).to_le_bytes());
        for value in &self.states {
            if ![value.position, value.velocity, value.effort]
                .iter()
                .all(|v| v.is_finite())
            {
                return Err(invalid("motor state must be finite"));
            }
            bytes.extend(value.index.to_le_bytes());
            for v in [value.position, value.velocity, value.effort] {
                bytes.extend(v.to_le_bytes());
            }
        }
        Ok(bytes)
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        if bytes.len() < 2 {
            return Err(invalid("truncated motor state"));
        }
        let count = u16::from_le_bytes(bytes[..2].try_into().unwrap()) as usize;
        if bytes.len() != 2 + 14 * count {
            return Err(invalid("invalid motor state length"));
        }
        let mut states = Vec::with_capacity(count);
        for value in bytes[2..].chunks_exact(14) {
            let floats: Vec<_> = value[2..]
                .chunks_exact(4)
                .map(|v| f32::from_le_bytes(v.try_into().unwrap()))
                .collect();
            if !floats.iter().all(|v| v.is_finite()) {
                return Err(invalid("motor state must be finite"));
            }
            states.push(MotorStateItem {
                index: u16::from_le_bytes(value[..2].try_into().unwrap()),
                position: floats[0],
                velocity: floats[1],
                effort: floats[2],
            });
        }
        motor_indices(states.iter().map(|v| v.index), count)?;
        Ok(Self { states })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum InputSection {
    Imu(ImuSample),
    Motor(MotorCommand),
    Adc(AdcInput),
    Voltage(AdcVoltage),
}
impl InputSection {
    fn encode(&self) -> Result<(u16, Vec<u8>)> {
        match self {
            Self::Imu(v) => Ok((1, v.encode()?)),
            Self::Motor(v) => Ok((2, v.encode()?)),
            Self::Adc(v) => Ok((3, v.encode()?)),
            Self::Voltage(v) => Ok((4, v.encode()?)),
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum StepInput {
    CompactImu(ImuSample),
    Sections(Vec<InputSection>),
}
impl StepInput {
    pub fn encode(&self) -> Result<Vec<u8>> {
        if let Self::CompactImu(sample) = self {
            let bytes = sample.encode()?;
            if u16::from_le_bytes(bytes[2..4].try_into().unwrap()) == 0x5343 {
                return Err(invalid(
                    "compact IMU conflicts with section discriminator; encode sections",
                ));
            }
            return Ok(bytes);
        }
        let Self::Sections(sections) = self else {
            unreachable!()
        };
        if sections.is_empty() || sections.len() > u16::MAX as usize {
            return Err(invalid("STEP requires sections"));
        }
        let mut bytes = Vec::from((sections.len() as u16).to_le_bytes());
        bytes.extend(0x5343u16.to_le_bytes());
        for section in sections {
            let (kind, payload) = section.encode()?;
            if bytes.len() + 8 + payload.len() > MAX_PAYLOAD {
                return Err(invalid("STEP payload exceeds 512 bytes"));
            }
            bytes.extend(kind.to_le_bytes());
            bytes.extend(0u16.to_le_bytes());
            bytes.extend((payload.len() as u32).to_le_bytes());
            bytes.extend(payload);
        }
        Self::decode(&bytes)?;
        Ok(bytes)
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        // SAFETY: C checks the input length before checking the discriminator.
        if unsafe { dm_mc02_v2_wire_is_compact_imu(bytes.as_ptr(), bytes.len()) } {
            return Ok(Self::CompactImu(ImuSample::decode(bytes)?));
        }
        let (mut count, mut marker) = (0, 0);
        // SAFETY: all pointers refer to live values; C validates the input size.
        if !unsafe {
            dm_mc02_v2_wire_step_payload_begin(bytes.as_ptr(), bytes.len(), &mut count, &mut marker)
        } {
            return Err(invalid("invalid STEP sections"));
        }
        let mut sections = Vec::new();
        let mut offset = 4;
        let mut singletons = [false; 2];
        let (mut raw_channels, mut voltage_channels) = (0u32, 0u32);
        for _ in 0..count {
            let mut native = NativeSection::default();
            // SAFETY: C validates each section against the original input and
            // returns a pointer into that input; no pointer is retained.
            if !unsafe {
                dm_mc02_v2_wire_next_section(bytes.as_ptr(), bytes.len(), &mut offset, &mut native)
            } {
                return Err(invalid("invalid STEP section length or flags"));
            }
            // SAFETY: successful C iteration proved this complete range lies
            // inside bytes, which remains immutably borrowed for this call.
            let value =
                unsafe { std::slice::from_raw_parts(native.payload, native.length as usize) };
            if native.kind <= 2 {
                let seen = &mut singletons[(native.kind - 1) as usize];
                if *seen {
                    return Err(invalid("duplicate STEP singleton section"));
                }
                *seen = true;
            }
            sections.push(match native.kind {
                1 => InputSection::Imu(ImuSample::decode(value)?),
                2 => InputSection::Motor(MotorCommand::decode(value)?),
                3 => {
                    let v = AdcInput::decode(value)?;
                    if raw_channels & (1 << v.channel) != 0 {
                        return Err(invalid("duplicate ADC raw channel"));
                    }
                    raw_channels |= 1 << v.channel;
                    InputSection::Adc(v)
                }
                4 => {
                    let v = AdcVoltage::decode(value)?;
                    if voltage_channels & (1 << v.channel) != 0 {
                        return Err(invalid("duplicate ADC voltage channel"));
                    }
                    voltage_channels |= 1 << v.channel;
                    InputSection::Voltage(v)
                }
                _ => return Err(invalid("unsupported STEP input section")),
            });
        }
        if offset != bytes.len() {
            return Err(invalid("STEP payload has trailing bytes"));
        }
        Ok(Self::Sections(sections))
    }
}

/// Legacy stream records remain supported for opening markers and Lab input.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct V1Frame {
    pub kind: u16,
    pub sequence: u64,
    pub time_ns: u64,
    pub payload: Vec<u8>,
}
#[repr(C)]
#[derive(Default)]
struct NativeV1Header {
    version: u16,
    kind: u16,
    payload_len: u32,
    sequence: u64,
    time_ns: u64,
}
#[repr(C)]
struct NativeV1Frame {
    header: NativeV1Header,
    payload: [u8; 128],
}
impl Default for NativeV1Frame {
    fn default() -> Self {
        Self {
            header: NativeV1Header::default(),
            payload: [0; 128],
        }
    }
}
unsafe extern "C" {
    fn dm_mc02_wire_encode(out: *mut u8, capacity: usize, frame: *const NativeV1Frame) -> usize;
    fn dm_mc02_wire_decode(frame: *mut NativeV1Frame, wire: *const u8, length: usize) -> bool;
    fn dm_mc02_wire_payload_valid(kind: u16, payload: *const u8, length: usize) -> bool;
}
impl V1Frame {
    pub fn encode_body(&self) -> Result<Vec<u8>> {
        // SAFETY: the borrowed input remains live; C checks typed sizes first.
        if !unsafe {
            dm_mc02_wire_payload_valid(self.kind, self.payload.as_ptr(), self.payload.len())
        } {
            return Err(invalid("invalid typed v1 payload"));
        }
        let mut frame = NativeV1Frame {
            header: NativeV1Header {
                version: 1,
                kind: self.kind,
                payload_len: self.payload.len() as u32,
                sequence: self.sequence,
                time_ns: self.time_ns,
            },
            ..NativeV1Frame::default()
        };
        frame.payload[..self.payload.len()].copy_from_slice(&self.payload);
        let mut bytes = vec![0; 28 + self.payload.len()];
        // SAFETY: matching repr(C) frame and output with exact bounded capacity.
        let size = unsafe { dm_mc02_wire_encode(bytes.as_mut_ptr(), bytes.len(), &frame) };
        if size == 0 {
            return Err(invalid("invalid v1 frame"));
        }
        bytes.truncate(size);
        Ok(bytes)
    }
    pub fn decode_body(bytes: &[u8]) -> Result<Self> {
        let mut frame = NativeV1Frame::default();
        // SAFETY: C checks body length before reading or populating fixed output.
        if !unsafe { dm_mc02_wire_decode(&mut frame, bytes.as_ptr(), bytes.len()) } {
            return Err(invalid("invalid v1 frame body"));
        }
        let payload = frame.payload[..frame.header.payload_len as usize].to_vec();
        // SAFETY: input is the owned bounded output of the checked frame decoder.
        if !unsafe {
            dm_mc02_wire_payload_valid(frame.header.kind, payload.as_ptr(), payload.len())
        } {
            return Err(invalid("invalid typed v1 payload"));
        }
        Ok(Self {
            kind: frame.header.kind,
            sequence: frame.header.sequence,
            time_ns: frame.header.time_ns,
            payload,
        })
    }
}
