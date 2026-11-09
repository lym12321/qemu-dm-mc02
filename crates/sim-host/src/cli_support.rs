use std::{
    io,
    sync::{atomic::AtomicBool, Arc},
    time::Duration,
};

pub fn seconds(text: &str) -> Result<Duration, String> {
    text.parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value > 0.0)
        .and_then(|value| Duration::try_from_secs_f64(value).ok())
        .filter(|value| !value.is_zero())
        .ok_or_else(|| "must be finite and positive".to_owned())
}

pub struct Signals {
    pub cancelled: Arc<AtomicBool>,
    #[cfg(unix)]
    registrations: Vec<signal_hook::SigId>,
}
impl Signals {
    pub fn new() -> io::Result<Self> {
        let guard = Self {
            cancelled: Arc::new(AtomicBool::new(false)),
            #[cfg(unix)]
            registrations: Vec::new(),
        };
        #[cfg(unix)]
        {
            let mut guard = guard;
            for signal in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
                guard.registrations.push(signal_hook::flag::register(
                    signal,
                    guard.cancelled.clone(),
                )?);
            }
            Ok(guard)
        }
        #[cfg(not(unix))]
        Ok(guard)
    }
}
impl Drop for Signals {
    fn drop(&mut self) {
        #[cfg(unix)]
        for id in self.registrations.drain(..) {
            signal_hook::low_level::unregister(id);
        }
    }
}

pub fn error_code(error: &sim_core::Failure) -> i32 {
    match error.downcast_ref::<sim_core::Error>() {
        Some(sim_core::Error::Interrupted(_)) => 130,
        Some(sim_core::Error::BackendUnavailable(_)) => 78,
        _ => 1,
    }
}

#[cfg(feature = "scene")]
pub fn error_type(error: &sim_core::Failure) -> &'static str {
    use sim_core::Error;
    #[cfg(all(unix, feature = "qmp"))]
    if let Some(error) = error.downcast_ref::<sim_host::qmp::Error>() {
        return match error.kind {
            sim_host::qmp::ErrorKind::Timeout => "TimeoutError",
            sim_host::qmp::ErrorKind::Admission => "ValueError",
            _ => "RuntimeError",
        };
    }
    #[cfg(all(unix, feature = "native-process"))]
    if let Some(error) = error.downcast_ref::<sim_host::native_process::Error>() {
        return match error.kind {
            sim_host::native_process::ErrorKind::Deadline => "TimeoutError",
            sim_host::native_process::ErrorKind::Admission => "ValueError",
            _ => "RuntimeError",
        };
    }
    match error.downcast_ref::<Error>() {
        Some(Error::BackendUnavailable(_)) => "BackendUnavailable",
        Some(Error::Interrupted(_)) => "KeyboardInterrupt",
        Some(Error::Deadline(_)) => "TimeoutError",
        Some(Error::Admission(_)) => "ValueError",
        Some(Error::Capacity(_)) => "BufferError",
        _ => "RuntimeError",
    }
}
