use super::{commands::QmpCommand, PreparedCommand};
use futures::{Future, FutureExt};
use qapi::futures::{
    QapiService, QmpPendingBatch, QmpPreparedBatch, QmpStreamTokio, QmpSubmitError,
};
use serde_json::Value;
use std::{
    fmt, io,
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::{
    io::WriteHalf,
    net::UnixStream,
    task::{JoinError, JoinHandle},
};

const RPC_LIMIT: Duration = Duration::from_secs(2);
const CLOSE_LIMIT: Duration = Duration::from_secs(2);
type Service = QapiService<QmpStreamTokio<WriteHalf<UnixStream>>>;
pub type Failure = Arc<Error>;
pub type Result<T> = std::result::Result<T, Failure>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Admission,
    State,
    Timeout,
    Transport,
    Command,
}

#[derive(Debug)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
    notes: Mutex<Vec<String>>,
    source: Option<Box<dyn std::error::Error + Send + Sync>>,
}

impl Error {
    fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            notes: Mutex::new(Vec::new()),
            source: None,
        }
    }
    fn io(error: io::Error) -> Self {
        let mut result = Self::new(ErrorKind::Transport, error.to_string());
        result.source = Some(Box::new(error));
        result
    }
    pub fn notes(&self) -> Vec<String> {
        self.notes.lock().unwrap().clone()
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.message.fmt(f)
    }
}
impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source.as_deref().map(|error| error as _)
    }
}

#[derive(Default)]
struct SessionState {
    first: Mutex<Option<Failure>>,
    closed: AtomicBool,
}
impl SessionState {
    fn fail(&self, error: Error) -> Failure {
        let mut first = self.first.lock().unwrap();
        first.get_or_insert_with(|| Arc::new(error)).clone()
    }
    fn check(&self) -> Result<()> {
        if let Some(error) = &*self.first.lock().unwrap() {
            return Err(error.clone());
        }
        if self.closed.load(Ordering::Relaxed) {
            return Err(Arc::new(Error::new(
                ErrorKind::State,
                "QMP session is closed",
            )));
        }
        Ok(())
    }
}

struct CancellationGuard {
    state: Arc<SessionState>,
    armed: bool,
    closing: bool,
}
impl CancellationGuard {
    fn new(state: &Arc<SessionState>) -> Self {
        Self {
            state: state.clone(),
            armed: true,
            closing: false,
        }
    }
    fn complete(&mut self) {
        self.armed = false;
    }
}
impl Drop for CancellationGuard {
    fn drop(&mut self) {
        if self.armed && (self.closing || !self.state.closed.load(Ordering::Relaxed)) {
            self.state.fail(Error::new(
                ErrorKind::State,
                if self.closing {
                    "QMP cleanup was interrupted"
                } else {
                    "QMP in-flight batch was abandoned"
                },
            ));
        }
    }
}

struct DriverTask(Option<JoinHandle<io::Result<()>>>);
impl Drop for DriverTask {
    fn drop(&mut self) {
        if let Some(handle) = &self.0 {
            handle.abort();
        }
    }
}

pub struct PreparedBatch(QmpPreparedBatch);
pub struct PendingBatch {
    pending: Option<QmpPendingBatch>,
    guard: CancellationGuard,
    deadline: Instant,
}

/// Owns one QMP connection and its response driver; borrows the caller's runtime.
pub struct QmpSession {
    service: Option<Service>,
    driver: DriverTask,
    state: Arc<SessionState>,
}

fn rpc_deadline(host_deadline: Instant) -> Instant {
    host_deadline.min(Instant::now() + RPC_LIMIT)
}
fn expired() -> Error {
    Error::new(ErrorKind::Timeout, "QMP deadline expired")
}
fn driver_error(result: std::result::Result<io::Result<()>, JoinError>) -> Error {
    match result {
        Ok(Err(error)) => Error::io(error),
        Ok(Ok(())) => Error::io(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "QMP connection closed",
        )),
        Err(error) => Error::new(
            ErrorKind::Transport,
            format!("QMP response driver failed: {error}"),
        ),
    }
}

async fn observe<T>(
    driver: &mut DriverTask,
    operation: impl Future<Output = std::result::Result<T, Error>>,
    deadline: Instant,
) -> std::result::Result<T, Error> {
    enum Outcome<T> {
        Reply(std::result::Result<T, Error>),
        Driver(std::result::Result<io::Result<()>, JoinError>),
    }
    if Instant::now() >= deadline {
        return Err(expired());
    }
    let handle = driver.0.as_mut().expect("active QMP driver");
    let outcome = tokio::time::timeout_at(deadline.into(), async {
        let operation = operation.fuse();
        let ended = handle.fuse();
        futures::pin_mut!(operation, ended);
        futures::select_biased! {
            result = ended => Outcome::Driver(result),
            result = operation => Outcome::Reply(result),
        }
    })
    .await
    .map_err(|_| expired())?;
    match outcome {
        Outcome::Reply(result) => result,
        Outcome::Driver(result) => {
            driver.0.take();
            Err(driver_error(result))
        }
    }
}

impl QmpSession {
    /// Socket, greeting and capability negotiation share one absolute deadline.
    pub async fn connect_uds(path: impl AsRef<Path>, host_deadline: Instant) -> Result<Self> {
        let deadline = rpc_deadline(host_deadline);
        if Instant::now() >= deadline {
            return Err(Arc::new(expired()));
        }
        let stream = tokio::time::timeout_at(deadline.into(), async {
            QmpStreamTokio::open_uds(path).await?.negotiate().await
        })
        .await
        .map_err(|_| Arc::new(expired()))?
        .map_err(|error| Arc::new(Error::io(error)))?;
        let (service, events) = stream.into_parts();
        events
            .release()
            .map_err(|_| Arc::new(Error::new(ErrorKind::State, "QMP driver already abandoned")))?;
        Ok(Self {
            service: Some(service),
            driver: DriverTask(Some(tokio::spawn(events))),
            state: Arc::new(SessionState::default()),
        })
    }

    pub fn prepare_batch(&self, commands: Vec<PreparedCommand>) -> Result<PreparedBatch> {
        self.state.check()?;
        self.service
            .as_ref()
            .unwrap()
            .prepare_batch(commands)
            .map(PreparedBatch)
            .map_err(|error| Arc::new(Error::new(ErrorKind::Admission, error.to_string())))
    }

    pub async fn submit_batch(
        &mut self,
        batch: PreparedBatch,
        host_deadline: Instant,
    ) -> Result<PendingBatch> {
        self.state.check()?;
        let deadline = rpc_deadline(host_deadline);
        let mut guard = CancellationGuard::new(&self.state);
        let submit = self
            .service
            .as_ref()
            .unwrap()
            .submit_batch(batch.0)
            .map(|result| {
                result.map_err(|error| match error {
                    QmpSubmitError::Admission(error) => {
                        Error::new(ErrorKind::Admission, error.to_string())
                    }
                    QmpSubmitError::Io(error) => Error::io(error),
                })
            });
        let result = observe(&mut self.driver, submit, deadline).await;
        match result {
            Ok(pending) => Ok(PendingBatch {
                pending: Some(pending),
                guard,
                deadline,
            }),
            Err(error) => {
                guard.complete();
                // Lower admission is atomic and has made no write.
                if error.kind == ErrorKind::Admission {
                    return Err(Arc::new(error));
                }
                Err(self.state.fail(error))
            }
        }
    }

    pub async fn finish_batch(
        &mut self,
        mut batch: PendingBatch,
        host_deadline: Instant,
    ) -> Result<Vec<Value>> {
        self.state.check()?;
        if !Arc::ptr_eq(&self.state, &batch.guard.state) {
            return Err(Arc::new(Error::new(
                ErrorKind::Admission,
                "QMP batch belongs to another session",
            )));
        }
        let pending = batch.pending.take().unwrap();
        let result = observe(
            &mut self.driver,
            async { Ok(pending.responses().await) },
            host_deadline.min(batch.deadline),
        )
        .await;
        batch.guard.complete();
        let replies = result.map_err(|error| self.state.fail(error))?;
        let mut values = Vec::with_capacity(replies.len());
        let mut first: Option<Error> = None;
        for (index, reply) in replies.into_iter().enumerate() {
            match reply {
                Ok(value) => values.push(value),
                Err(error) => {
                    let message = format!("QMP command {index}: {error}");
                    if let Some(first) = &first {
                        first.notes.lock().unwrap().push(message);
                    } else {
                        let kind = match error {
                            qapi::ExecuteError::Qapi(_) => ErrorKind::Command,
                            qapi::ExecuteError::Io(_) => ErrorKind::Transport,
                        };
                        let mut failure = Error::new(kind, message);
                        failure.source = Some(Box::new(error));
                        first = Some(failure);
                    }
                }
            }
        }
        if let Some(error) = first {
            return Err(self.state.fail(error));
        }
        Ok(values)
    }

    pub async fn command_many(
        &mut self,
        commands: Vec<PreparedCommand>,
        host_deadline: Instant,
    ) -> Result<Vec<Value>> {
        let batch = self.prepare_batch(commands)?;
        let batch = self.submit_batch(batch, host_deadline).await?;
        self.finish_batch(batch, host_deadline).await
    }

    pub async fn command<C: QmpCommand>(
        &mut self,
        command: C,
        host_deadline: Instant,
    ) -> Result<C::Ok> {
        let command = PreparedCommand::new(command)
            .map_err(|error| Arc::new(Error::new(ErrorKind::Admission, error.to_string())))?;
        let mut values = self.command_many(vec![command], host_deadline).await?;
        serde_json::from_value(values.remove(0)).map_err(|error| {
            self.state.fail(Error::new(
                ErrorKind::Transport,
                format!("invalid QMP command response: {error}"),
            ))
        })
    }

    /// Cleanup has a separate finite budget and retains any earlier failure.
    pub async fn close(&mut self) -> Result<()> {
        let mut guard = CancellationGuard::new(&self.state);
        guard.closing = true;
        self.state.closed.store(true, Ordering::Relaxed);
        self.service.take();
        let mut driver = DriverTask(self.driver.0.take());
        if let Some(handle) = driver.0.as_mut() {
            match tokio::time::timeout(CLOSE_LIMIT, handle).await {
                Ok(result) => {
                    driver.0.take();
                    if !matches!(result, Ok(Ok(()))) {
                        let error = driver_error(result);
                        let first = self.state.first.lock().unwrap().clone();
                        if let Some(first) = first {
                            first
                                .notes
                                .lock()
                                .unwrap()
                                .push(format!("QMP cleanup: {error}"));
                        } else {
                            self.state.fail(error);
                        }
                    }
                }
                Err(_) => {
                    let first = self
                        .state
                        .fail(Error::new(ErrorKind::Timeout, "QMP close deadline expired"));
                    first
                        .notes
                        .lock()
                        .unwrap()
                        .push("QMP response driver aborted".into());
                }
            }
        }
        guard.complete();
        let first = self.state.first.lock().unwrap();
        match &*first {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}

impl Drop for QmpSession {
    fn drop(&mut self) {
        self.state.closed.store(true, Ordering::Relaxed);
        self.service.take();
    }
}
