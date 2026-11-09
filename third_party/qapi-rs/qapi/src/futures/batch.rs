//! Local async QMP extension. The envelope remains qapi-spec's Execute.
use super::{PendingRequest, QapiService, QapiShared};
use crate::{Any, Execute, ExecuteError};
use futures::{channel::oneshot, future::join_all, Sink, SinkExt};
use qapi_qmp::QmpCommand;
use std::{io, sync::Arc};

pub const QMP_MAX_FRAME_LENGTH: usize = 10 * 1024 * 1024;
pub const QMP_MAX_PENDING: usize = 8;

/// An owned snapshot: user serialization runs before submission and any IO.
pub struct QmpPreparedCommand(Any);

impl QmpPreparedCommand {
    pub fn new<C: QmpCommand>(command: C) -> io::Result<Self> {
        serde_json::to_value(Execute::new(command, Option::<u32>::None))
            .map(Self)
            .map_err(From::from)
    }
}

pub struct QmpPreparedFrame(pub(super) Vec<u8>);

pub struct QmpPreparedBatch {
    shared: Arc<QapiShared>,
    frames: Vec<(u32, QmpPreparedFrame)>,
}

#[derive(Debug)]
pub enum QmpSubmitError {
    Admission(io::Error),
    Io(io::Error),
}

impl From<io::Error> for QmpSubmitError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Dropping this value unregisters every request, including after partial IO.
pub struct QmpPendingBatch(Vec<PendingRequest>);

impl QmpPendingBatch {
    pub async fn responses(self) -> Vec<Result<Any, ExecuteError>> {
        join_all(self.0).await
    }
}

impl<W> QapiService<W> {
    pub fn prepare_batch(&self, commands: Vec<QmpPreparedCommand>) -> io::Result<QmpPreparedBatch> {
        if !self.shared.uses_ids || commands.is_empty() || commands.len() > QMP_MAX_PENDING {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "QMP batch must contain 1..8 commands",
            ));
        }
        let first = self.reserve_ids(commands.len())?;
        let mut frames = Vec::with_capacity(commands.len());
        for (offset, QmpPreparedCommand(mut command)) in commands.into_iter().enumerate() {
            let id = first + offset as u32;
            // Execute serialization always produces this object. The snapshot
            // no longer contains a user-controlled serializer.
            command
                .as_object_mut()
                .unwrap()
                .insert("id".into(), id.into());
            let mut bytes = serde_json::to_vec(&command)?;
            if bytes.len() >= QMP_MAX_FRAME_LENGTH {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "QMP frame exceeds limit",
                ));
            }
            bytes.push(b'\n');
            frames.push((id, QmpPreparedFrame(bytes)));
        }
        Ok(QmpPreparedBatch {
            shared: self.shared.clone(),
            frames,
        })
    }

    pub async fn submit_batch(
        &self,
        batch: QmpPreparedBatch,
    ) -> Result<QmpPendingBatch, QmpSubmitError>
    where
        W: Sink<QmpPreparedFrame, Error = io::Error> + Unpin,
    {
        if !Arc::ptr_eq(&self.shared, &batch.shared) {
            return Err(QmpSubmitError::Admission(io::Error::new(
                io::ErrorKind::InvalidInput,
                "QMP batch belongs to another session",
            )));
        }
        let mut sink = self.write.lock().await;
        let pending = {
            let mut commands = self.shared.commands.lock().unwrap();
            if commands.abandoned {
                return Err(
                    io::Error::new(io::ErrorKind::BrokenPipe, "QMP stream disconnected").into(),
                );
            }
            if commands.pending.len() + batch.frames.len() > QMP_MAX_PENDING {
                return Err(QmpSubmitError::Admission(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "QMP pending request capacity exceeded",
                )));
            }
            let mut pending = Vec::with_capacity(batch.frames.len());
            for (id, _) in &batch.frames {
                let (sender, receiver) = oneshot::channel();
                commands.pending.insert(*id, sender);
                pending.push(PendingRequest {
                    id: *id,
                    receiver,
                    shared: self.shared.clone(),
                });
            }
            QmpPendingBatch(pending)
        };
        for (_, frame) in batch.frames {
            sink.feed(frame).await?;
        }
        sink.flush().await?;
        Ok(pending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::Ordering;

    #[test]
    fn request_id_exhaustion_does_not_wrap() {
        let service = QapiService::new((), Arc::new(QapiShared::new(true)));
        service.id_counter.store(u32::MAX as u64, Ordering::Relaxed);
        assert!(service
            .prepare_batch(vec![
                QmpPreparedCommand::new(qapi_qmp::query_status {}).unwrap(),
                QmpPreparedCommand::new(qapi_qmp::query_status {}).unwrap()
            ])
            .is_err());
        assert!(service
            .prepare_batch(vec![
                QmpPreparedCommand::new(qapi_qmp::query_status {}).unwrap()
            ])
            .is_ok());
        assert!(service
            .prepare_batch(vec![
                QmpPreparedCommand::new(qapi_qmp::query_status {}).unwrap()
            ])
            .is_err());
    }
}
