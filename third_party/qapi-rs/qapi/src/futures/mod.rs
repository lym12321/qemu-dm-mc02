#[cfg(feature = "qapi-qmp")]
use qapi_qmp::{QMPCapability, QapiCapabilities, QmpMessage};

use crate::{Any, Command, Execute, ExecuteResult};
use qapi_spec::Response;

use futures::channel::oneshot;
use futures::lock::Mutex;
use futures::task::AtomicWaker;
use futures::{Future, FutureExt, Sink, SinkExt, Stream};
use log::{info, trace, warn};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::convert::{TryFrom, TryInto};
use std::io;
use std::marker::Unpin;
use std::pin::Pin;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Mutex as StdMutex,
};
use std::task::{Context, Poll};

#[cfg(feature = "tokio-util")]
mod codec;

#[cfg(feature = "qapi-qmp")]
mod batch;
#[cfg(feature = "qapi-qmp")]
pub use batch::*;

#[cfg(feature = "tokio")]
mod tokio;
#[cfg(feature = "tokio")]
pub use self::tokio::*;

#[cfg(feature = "tower-service")]
mod tower;

pub struct QapiStream<R, W> {
    service: QapiService<W>,
    events: QapiEvents<R>,
}

impl<R, W> QapiStream<R, W> {
    pub fn with_parts(service: QapiService<W>, events: QapiEvents<R>) -> Self {
        Self { service, events }
    }

    pub fn into_parts(self) -> (QapiService<W>, QapiEvents<R>) {
        (self.service, self.events)
    }

    #[cfg(feature = "async-tokio-spawn")]
    pub fn spawn_tokio(self) -> (QapiService<W>, ::tokio::task::JoinHandle<()>)
    where
        QapiEvents<R>: Future<Output = io::Result<()>> + Send + 'static,
    {
        let handle = self.events.spawn_tokio();
        (self.service, handle)
    }

    pub fn execute<'a, C: Command + 'a>(
        &'a mut self,
        command: C,
    ) -> impl Future<Output = ExecuteResult<C>> + 'a
    where
        QapiEvents<R>: Future<Output = io::Result<()>> + Unpin,
        W: Sink<Execute<C, u32>, Error = io::Error> + Unpin,
    {
        let execute = self.service.execute(command).fuse();

        async move {
            futures::pin_mut!(execute);

            futures::select_biased! {
                res = execute => res,
                res = (&mut self.events).fuse() => {
                    res?;
                    Err(io::Error::new(io::ErrorKind::UnexpectedEof, "unexpected EOF when executing command").into())
                },
            }
        }
    }
}

#[cfg(feature = "qapi-qmp")]
pub struct QmpStreamNegotiation<S, W> {
    pub stream: QapiStream<S, W>,
    pub capabilities: QapiCapabilities,
}

#[cfg(feature = "qapi-qmp")]
impl<S, W> QmpStreamNegotiation<S, W>
where
    QapiEvents<S>: Future<Output = io::Result<()>> + Unpin,
    W: Sink<Execute<qapi_qmp::qmp_capabilities, u32>, Error = io::Error> + Unpin,
{
    pub async fn negotiate_caps<C>(mut self, caps: C) -> io::Result<QapiStream<S, W>>
    where
        C: IntoIterator<Item = QMPCapability>,
    {
        let _ = self
            .stream
            .execute(qapi_qmp::qmp_capabilities {
                enable: Some(caps.into_iter().collect()),
            })
            .await?;

        Ok(self.stream)
    }

    pub async fn negotiate(self) -> io::Result<QapiStream<S, W>> {
        self.negotiate_caps(std::iter::empty()).await
    }
}

type QapiCommandMap = BTreeMap<u32, oneshot::Sender<Result<Any, qapi_spec::Error>>>;

pub struct QapiService<W> {
    shared: Arc<QapiShared>,
    write: Arc<Mutex<W>>,
    id_counter: AtomicU64,
}

impl<W> QapiService<W> {
    #[cfg(feature = "tokio")]
    fn new(write: W, shared: Arc<QapiShared>) -> Self {
        QapiService {
            shared,
            write: Mutex::new(write).into(),
            id_counter: AtomicU64::new(0),
        }
    }

    fn reserve_ids(&self, count: usize) -> io::Result<u32> {
        self.id_counter
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(count as u64)
                    .filter(|end| *end <= u32::MAX as u64 + 1)
            })
            .map(|first| first as u32)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "QMP request IDs exhausted"))
    }

    fn command_id(&self) -> io::Result<Option<u32>> {
        if self.shared.uses_ids {
            self.reserve_ids(1).map(Some)
        } else {
            Ok(None)
        }
    }

    fn command_response<C: Command>(
        pending: PendingRequest,
    ) -> impl Future<Output = ExecuteResult<C>> {
        pending.map(|res| match res {
            Ok(res) => C::Ok::deserialize(&res)
                .map_err(io::Error::from)
                .map_err(From::from),
            Err(e) => Err(e),
        })
    }

    pub fn execute<C: Command>(&self, command: C) -> impl Future<Output = ExecuteResult<C>>
    where
        W: Sink<Execute<C, u32>, Error = io::Error> + Unpin,
    {
        let id = self.command_id();
        let sink = self.write.clone();
        let shared = self.shared.clone();

        async move {
            let id = id?;
            let command = Execute::new(command, id);
            let mut sink = sink.lock().await;
            let pending = shared.command_insert(id.unwrap_or_default())?;

            sink.send(command).await?;
            if id.is_some() {
                // QMP IDs are independent of OOB; QGA remains serialized.
                drop(sink)
            }

            Self::command_response::<C>(pending).await
        }
    }

    /*pub async fn execute_oob<C: Command>(&self, command: C) -> io::Result<ExecuteResult<C>> {
        /* TODO: should we assert C::ALLOW_OOB here and/or at the type level?
         * If oob isn't supported should we fall back to serial execution or error?
         */
        self.execute_(command, true).await
    }*/

    #[cfg(feature = "qapi-qga")]
    pub fn guest_sync(
        &self,
        sync_value: i32,
    ) -> impl Future<Output = Result<(), crate::ExecuteError>>
    where
        W: Sink<Execute<qapi_qga::guest_sync, u32>, Error = io::Error> + Unpin,
    {
        let id = sync_value.into();
        self.execute(qapi_qga::guest_sync { id }).map(move |res| {
            res.and_then(|res| {
                if res == id {
                    Ok(())
                } else {
                    Err(io::Error::new(io::ErrorKind::InvalidData, "QGA sync failed").into())
                }
            })
        })
    }

    fn stop(&self) {
        let mut commands = self.shared.commands.lock().unwrap();
        if self.shared.abandoned.load(Ordering::Relaxed) {
            self.shared.stop();
        }
        commands.abandoned = true;
    }
}

impl<W> Drop for QapiService<W> {
    fn drop(&mut self) {
        self.stop();
    }
}

#[derive(Default)]
struct QapiSharedCommands {
    pending: QapiCommandMap,
    abandoned: bool,
}

struct QapiShared {
    commands: StdMutex<QapiSharedCommands>,
    stop_waker: AtomicWaker,
    stop: AtomicBool,
    abandoned: AtomicBool,
    uses_ids: bool,
}

impl QapiShared {
    #[cfg(feature = "tokio")]
    fn new(uses_ids: bool) -> Self {
        Self {
            commands: Default::default(),
            stop_waker: Default::default(),
            stop: Default::default(),
            abandoned: Default::default(),
            uses_ids,
        }
    }

    fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        self.stop_waker.wake();
    }

    fn is_stopped(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    fn poll_next<T, P: FnOnce(&mut Context) -> Poll<Option<T>>>(
        &self,
        cx: &mut Context,
        poll: P,
    ) -> Poll<Option<T>> {
        if self.is_stopped() {
            return Poll::Ready(None);
        }

        // attempt to complete the future
        match poll(cx) {
            Poll::Ready(res) => {
                if res.is_none() {
                    self.stop.store(true, Ordering::Relaxed);
                }
                Poll::Ready(res)
            }
            Poll::Pending => {
                self.stop_waker.register(cx.waker());
                if self.is_stopped() {
                    Poll::Ready(None)
                } else {
                    Poll::Pending
                }
            }
        }
    }

    fn command_remove(&self, id: u32) -> Option<oneshot::Sender<Result<Any, qapi_spec::Error>>> {
        let mut commands = self.commands.lock().unwrap();
        commands.pending.remove(&id)
    }

    fn command_insert(self: &Arc<Self>, id: u32) -> io::Result<PendingRequest> {
        let (sender, receiver) = oneshot::channel();
        let mut commands = self.commands.lock().unwrap();
        if commands.abandoned {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "QAPI stream disconnected",
            ));
        }
        if commands.pending.contains_key(&id) || (self.uses_ids && commands.pending.len() >= 8) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "QMP pending request capacity exceeded",
            ));
        }
        commands.pending.insert(id, sender);
        Ok(PendingRequest {
            id,
            receiver,
            shared: self.clone(),
        })
    }
}

struct PendingRequest {
    id: u32,
    receiver: oneshot::Receiver<Result<Any, qapi_spec::Error>>,
    shared: Arc<QapiShared>,
}

impl Future for PendingRequest {
    type Output = Result<Any, crate::ExecuteError>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        Pin::new(&mut self.receiver)
            .poll(cx)
            .map(|result| match result {
                Ok(result) => result.map_err(From::from),
                Err(_) => Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "QAPI stream disconnected",
                )
                .into()),
            })
    }
}

impl Drop for PendingRequest {
    fn drop(&mut self) {
        self.shared.command_remove(self.id);
    }
}

#[must_use]
pub struct QapiEvents<S> {
    stream: S,
    shared: Arc<QapiShared>,
}

impl<S> QapiEvents<S> {
    pub fn release(&self) -> Result<(), ()> {
        let commands = self.shared.commands.lock().unwrap();
        if commands.abandoned {
            Err(())
        } else {
            self.shared.abandoned.store(true, Ordering::Relaxed);
            Ok(())
        }
    }

    pub async fn into_future(self) -> ()
    where
        Self: Future<Output = io::Result<()>>,
    {
        if self.release().is_err() {
            info!("QAPI service abandoned before spawning");
            return;
        }

        match self.await {
            Ok(()) => (),
            Err(e) => warn!("QAPI stream closed with error {:?}", e),
        }
    }

    pub fn spawn<SP: futures::task::Spawn>(self, spawn: SP) -> Result<(), futures::task::SpawnError>
    where
        Self: Future<Output = io::Result<()>> + Send + 'static,
        S: 'static,
    {
        use futures::task::SpawnExt;

        spawn.spawn(self.into_future())
    }

    #[cfg(feature = "async-tokio-spawn")]
    pub fn spawn_tokio(self) -> ::tokio::task::JoinHandle<()>
    where
        Self: Future<Output = io::Result<()>> + Send + 'static,
        S: 'static,
    {
        ::tokio::spawn(self.into_future())
    }
}

impl<S> Drop for QapiEvents<S> {
    fn drop(&mut self) {
        let mut commands = self.shared.commands.lock().unwrap();
        commands.pending.clear();
        commands.abandoned = true;
    }
}

fn handle_response(shared: &QapiShared, res: Response<Any>) -> io::Result<()> {
    let id = if shared.uses_ids {
        if res.id().is_none() && matches!(res, Response::Err(_)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "QMP error without request ID",
            ));
        }
        match res
            .id()
            .and_then(|id| id.as_u64())
            .and_then(|id| u32::try_from(id).ok())
        {
            Some(id) => id,
            None => return Ok(()), // QMP specifies dropping replies with unknown IDs.
        }
    } else {
        0
    };

    if let Some(sender) = shared.command_remove(id) {
        let _ = sender.send(res.result()); // A cancelled owner has already abandoned this reply.
    }
    Ok(())
}

impl<M, S> Future for QapiEvents<S>
where
    S: Stream<Item = io::Result<M>>,
    M: TryInto<Response<Any>>,
{
    type Output = io::Result<()>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = unsafe { self.get_unchecked_mut() };
        let stream = unsafe { Pin::new_unchecked(&mut this.stream) };
        let shared = &this.shared;

        shared
            .poll_next(cx, |cx| {
                Poll::Ready(Some(match futures::ready!(stream.poll_next(cx)) {
                    None => return Poll::Ready(None),
                    Some(Err(e)) => Err(e),
                    Some(Ok(res)) => match res.try_into() {
                        Ok(res) => match handle_response(shared, res) {
                            Err(e) => Err(e),
                            Ok(()) => {
                                cx.waker().wake_by_ref(); // TODO: I've seen this not work with tokio?
                                return Poll::Pending;
                            }
                        },
                        Err(..) => {
                            trace!("Ignoring QAPI event");
                            cx.waker().wake_by_ref(); // TODO: I've seen this not work with tokio?
                            return Poll::Pending;
                        }
                    },
                }))
            })
            .map(|res| res.unwrap_or(Ok(())))
    }
}

#[cfg(feature = "qapi-qmp")]
impl<E, S: Stream<Item = io::Result<QmpMessage<Any, E>>>> Stream for QapiEvents<S> {
    type Item = io::Result<E>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = unsafe { self.get_unchecked_mut() };
        let stream = unsafe { Pin::new_unchecked(&mut this.stream) };
        let shared = &this.shared;

        shared.poll_next(cx, |cx| {
            Poll::Ready(match futures::ready!(stream.poll_next(cx)) {
                None => None, // eof
                Some(Err(e)) => Some(Err(e)),
                Some(Ok(QmpMessage::Event(e))) => Some(Ok(e)),
                Some(Ok(QmpMessage::Response(res))) => match handle_response(shared, res) {
                    Err(e) => Some(Err(e)),
                    Ok(()) => {
                        cx.waker().wake_by_ref(); // TODO: I've seen this not work with tokio?
                        return Poll::Pending;
                    }
                },
            })
        })
    }
}
