use std::{error, fmt, sync::Arc};

/// The original error allocation is preserved across adapter boundaries.
pub type Failure = Arc<dyn error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, Failure>;

#[derive(Debug)]
pub enum Error {
    Admission(String),
    State(String),
    Capacity(String),
    Deadline(String),
    BackendUnavailable(String),
    Interrupted(String),
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Admission(message)
            | Self::State(message)
            | Self::Capacity(message)
            | Self::Deadline(message)
            | Self::BackendUnavailable(message)
            | Self::Interrupted(message) => formatter.write_str(message),
        }
    }
}

impl error::Error for Error {}

pub(crate) fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}

pub(crate) fn state_error(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Session {
    pub session_id: u64,
    pub t_sim_ns: u64,
}

impl Session {
    pub fn new(session_id: u64, t_sim_ns: u64) -> Result<Self> {
        if session_id == 0 {
            return Err(admission("session ID must be positive"));
        }
        Ok(Self {
            session_id,
            t_sim_ns,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StepContext {
    pub session_id: u64,
    pub step_id: u64,
    pub t_sim_ns: u64,
    pub dt_ns: u64,
}

impl StepContext {
    pub fn new(session_id: u64, step_id: u64, t_sim_ns: u64, dt_ns: u64) -> Result<Self> {
        Session::new(session_id, t_sim_ns)?;
        if step_id == 0 || dt_ns == 0 || dt_ns > t_sim_ns {
            return Err(admission(
                "step ID and interval must be positive and fit the target",
            ));
        }
        Ok(Self {
            session_id,
            step_id,
            t_sim_ns,
            dt_ns,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StepStatus {
    Ok,
    Paused,
}

#[derive(Clone, Debug, PartialEq)]
pub struct StepResult<E> {
    pub status: StepStatus,
    pub reached_time_ns: u64,
    pub diagnostics: Option<E>,
}

impl<E> StepResult<E> {
    pub fn reached(reached_time_ns: u64) -> Self {
        Self {
            status: StepStatus::Ok,
            reached_time_ns,
            diagnostics: None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimeQuality {
    Exact,
    Sampled,
    LowerBound,
    UpperBound,
}

/// Input observations retain the admitted input without converting it into a
/// domain observation type in the scheduler.
#[derive(Clone, Debug, PartialEq)]
pub enum EventPayload<I, E> {
    Input(I),
    Observation(E),
}

#[derive(Clone, Debug, PartialEq)]
pub struct Event<I, E> {
    pub session_id: u64,
    pub sequence: u64,
    pub source_node: String,
    pub source_port: Option<String>,
    pub kind: String,
    pub payload: EventPayload<I, E>,
    pub source_time_ns: Option<u64>,
    pub mapped_time_ns: u64,
    pub delivery_time_ns: u64,
    pub quality: TimeQuality,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct EventTime {
    pub source_time_ns: Option<u64>,
    pub mapped_time_ns: Option<u64>,
    pub delivery_time_ns: Option<u64>,
    pub quality: Option<TimeQuality>,
}
