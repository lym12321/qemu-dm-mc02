#![cfg(all(any(unix, windows), feature = "qmp"))]

use futures::{channel::oneshot, Future, FutureExt};
use serde_json::{json, Value};
use sim_host::qmp::{
    commands, ErrorKind, PreparedCommand, QmpSession, QueryVirtualExecution, RunToVirtualTime,
    VirtualExecutionState, QMP_MAX_FRAME_LENGTH,
};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, ReadHalf, WriteHalf};
#[cfg(windows)]
use {
    std::net::SocketAddr as Address,
    tokio::net::{TcpListener as Listener, TcpStream as Stream},
};
#[cfg(unix)]
use {
    std::path::PathBuf as Address,
    tokio::net::{UnixListener as Listener, UnixStream as Stream},
};

async fn connect(address: Address, deadline: Instant) -> sim_host::qmp::Result<QmpSession> {
    #[cfg(unix)]
    {
        QmpSession::connect_uds(address, deadline).await
    }
    #[cfg(windows)]
    {
        let stream = Stream::connect(address).await.unwrap().into_std().unwrap();
        QmpSession::from_tcp(stream, deadline).await
    }
}

fn deadline() -> Instant {
    Instant::now() + Duration::from_secs(3)
}
fn prepared<C: commands::QmpCommand>(command: C) -> PreparedCommand {
    PreparedCommand::new(command).unwrap()
}
fn run(future: impl Future<Output = ()>) {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            tokio::time::timeout(Duration::from_secs(8), future)
                .await
                .unwrap()
        });
}

struct Peer {
    read: BufReader<ReadHalf<Stream>>,
    write: WriteHalf<Stream>,
}
impl Peer {
    async fn accept(listener: Listener) -> Self {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, write) = tokio::io::split(socket);
        Self {
            read: BufReader::new(read),
            write,
        }
    }
    async fn raw(&mut self, bytes: &[u8]) {
        self.write.write_all(bytes).await.unwrap();
    }
    async fn request(&mut self) -> Value {
        let mut line = String::new();
        assert_ne!(self.read.read_line(&mut line).await.unwrap(), 0);
        serde_json::from_str(&line).unwrap()
    }
    async fn reply(&mut self, request: &Value, value: Value) {
        self.raw(format!("{}\r\n", json!({"return":value,"id":request["id"]})).as_bytes())
            .await;
    }
    async fn greeting(&mut self) {
        // Independent QMP literals: no OOB advertised, unknown capability allowed.
        let greeting = b"{\"QMP\":{\"version\":{\"qemu\":{\"major\":8,\"minor\":2,\"micro\":2},\"package\":\"fixture\"},\"capabilities\":[\"future-cap\"]}}\r\n";
        self.raw(&greeting[..11]).await;
        tokio::task::yield_now().await;
        self.raw(&greeting[11..]).await;
    }
    async fn negotiate(&mut self) {
        self.greeting().await;
        let command = self.request().await;
        assert_eq!(
            command,
            json!({"execute":"qmp_capabilities","arguments":{"enable":[]},"id":0})
        );
        self.raw(
            b"{\"event\":\"FUTURE_EVENT\",\"timestamp\":\"discarded\",\"data\":{\"new\":true}}\n",
        )
        .await;
        self.reply(&command, json!({})).await;
    }
    async fn eof(&mut self) {
        let mut bytes = Vec::new();
        self.read.read_to_end(&mut bytes).await.unwrap();
        assert!(
            bytes.is_empty(),
            "unexpected request bytes: {}",
            String::from_utf8_lossy(&bytes)
        );
    }
}

async fn socket_case<S, SF, C, CF>(server: S, client: C)
where
    S: FnOnce(Peer) -> SF + Send + 'static,
    SF: Future<Output = ()> + Send + 'static,
    C: FnOnce(Address) -> CF,
    CF: Future<Output = ()>,
{
    #[cfg(unix)]
    let directory = tempfile::tempdir().unwrap();
    #[cfg(unix)]
    let path = directory.path().join("qmp.sock");
    #[cfg(unix)]
    let listener = Listener::bind(&path).unwrap();
    #[cfg(windows)]
    let listener = Listener::bind("127.0.0.1:0").await.unwrap();
    #[cfg(windows)]
    let path = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { server(Peer::accept(listener).await).await });
    client(path).await;
    server.await.unwrap();
}

#[test]
fn no_oob_batch_waits_for_all_requests_and_restores_input_order() {
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            let a = peer.request().await;
            let b = peer.request().await;
            assert_eq!(a, json!({"execute":"query-status","arguments":{},"id":1}));
            assert_eq!(
                b,
                json!({"execute":"x-query-virtual-execution","arguments":{},"id":2})
            );
            peer.raw(b"{\"event\":\"NEW_EVENT\"}\n{\"return\":{},\"id\":\"foreign\"}\n{\"return\":{},\"id\":-1}\n{\"return\":{},\"id\":4294967296}\n{\"return\":{},\"id\":99}\n{\"return\":{}}\n").await;
            let reversed = b"{\"return\":{\"current-ns\":123,\"state\":\"reached\",\"target-ns\":123},\"id\":2}\r\n{\"return\":{\"running\":false,\"singlestep\":false,\"status\":\"paused\"},\"id\":1}\n";
            for chunk in reversed.chunks(7) {
                peer.raw(chunk).await;
                tokio::task::yield_now().await;
            }
            let run = peer.request().await;
            assert_eq!(
                run,
                json!({"execute":"x-run-to-virtual-time","arguments":{"target-ns":124},"id":3})
            );
            peer.reply(&run, json!({})).await;
            let query = peer.request().await;
            peer.reply(
                &query,
                json!({"current-ns":123,"state":"running","target-ns":124}),
            )
            .await;
            let status = peer.request().await;
            peer.reply(
                &status,
                json!({"running":false,"singlestep":false,"status":"paused"}),
            )
            .await;
            peer.eof().await;
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            let result = session
                .command_many(
                    vec![
                        prepared(commands::query_status {}),
                        prepared(QueryVirtualExecution {}),
                    ],
                    deadline(),
                )
                .await
                .unwrap();
            assert_eq!(
                result,
                vec![
                    json!({"running":false,"singlestep":false,"status":"paused"}),
                    json!({"current-ns":123,"state":"reached","target-ns":123})
                ]
            );
            session
                .command(RunToVirtualTime { target_ns: 124 }, deadline())
                .await
                .unwrap();
            // Admission acknowledgement does not claim the virtual target completed.
            let state = session
                .command(QueryVirtualExecution {}, deadline())
                .await
                .unwrap();
            assert_eq!(state.state, VirtualExecutionState::Running);
            let status = session
                .command(commands::query_status {}, deadline())
                .await
                .unwrap();
            assert!(!status.running);
            session.close().await.unwrap();
        },
    ));
}

#[test]
fn command_errors_wait_for_every_reply_and_keep_input_position_primary() {
    let (errors_sent, ready) = oneshot::channel();
    let (release, released) = oneshot::channel();
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            let a = peer.request().await;
            let b = peer.request().await;
            let c = peer.request().await;
            peer.raw(
                format!(
                    "{}\n{}\n",
                    json!({"error":{"class":"GenericError","desc":"second"},"id":b["id"]}),
                    json!({"error":{"class":"CommandNotFound","desc":"first"},"id":a["id"]})
                )
                .as_bytes(),
            )
            .await;
            errors_sent.send(()).unwrap();
            released.await.unwrap();
            peer.reply(&c, json!({})).await;
            peer.eof().await;
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            let mut result = Box::pin(
                session.command_many(
                    (0..3)
                        .map(|_| prepared(commands::query_status {}))
                        .collect(),
                    deadline(),
                ),
            );
            let ready = ready.fuse();
            futures::pin_mut!(ready);
            futures::select_biased! { _ = result.as_mut().fuse() => panic!("returned before final reply"), _ = ready => () }
            assert!(result.as_mut().now_or_never().is_none());
            release.send(()).unwrap();
            let error = result.await.unwrap_err();
            assert_eq!(error.kind, ErrorKind::Command);
            assert!(error.message.contains("command 0") && error.message.contains("first"));
            assert_eq!(error.notes().len(), 1);
            assert!(error.notes()[0].contains("command 1") && error.notes()[0].contains("second"));
            let next = session
                .command(commands::query_status {}, deadline())
                .await
                .unwrap_err();
            assert!(Arc::ptr_eq(&error, &next));
            assert!(Arc::ptr_eq(&error, &session.close().await.unwrap_err()));
        },
    ));
}

#[test]
fn batch_admission_and_capacity_reject_without_partial_requests() {
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            let mut requests = Vec::new();
            for _ in 0..8 {
                requests.push(peer.request().await);
            }
            for request in requests {
                peer.reply(&request, json!({})).await;
            }
            let next = peer.request().await;
            assert_eq!(next["execute"], "x-query-virtual-execution");
            peer.reply(&next, json!({"current-ns":0,"state":"idle"}))
                .await;
            peer.eof().await;
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            assert!(session.prepare_batch(Vec::new()).is_err());
            assert!(session
                .prepare_batch(
                    (0..9)
                        .map(|_| prepared(commands::query_status {}))
                        .collect()
                )
                .is_err());
            let oversized = prepared(commands::qom_set {
                path: "/machine".into(),
                property: "test".into(),
                value: Value::String("x".repeat(QMP_MAX_FRAME_LENGTH)),
            });
            assert!(session
                .prepare_batch(vec![prepared(commands::query_status {}), oversized])
                .is_err());
            let batch = session
                .prepare_batch(
                    (0..8)
                        .map(|_| prepared(commands::query_status {}))
                        .collect(),
                )
                .unwrap();
            let batch = session.submit_batch(batch, deadline()).await.unwrap();
            let extra = session
                .prepare_batch(vec![prepared(commands::query_status {})])
                .unwrap();
            let error = match session.submit_batch(extra, deadline()).await {
                Err(error) => error,
                Ok(_) => panic!("exceeded pending capacity"),
            };
            assert_eq!(error.kind, ErrorKind::Admission);
            assert_eq!(
                session.finish_batch(batch, deadline()).await.unwrap().len(),
                8
            );
            session
                .command(QueryVirtualExecution {}, deadline())
                .await
                .unwrap();
            session.close().await.unwrap();
        },
    ));
}

#[test]
fn lower_cancellation_reclaims_capacity_and_drops_late_replies() {
    use qapi::futures::QmpStreamTokio;
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            let mut old = Vec::new();
            for _ in 0..8 {
                old.push(peer.request().await);
            }
            let mut current = Vec::new();
            for _ in 0..8 {
                current.push(peer.request().await);
            }
            for request in old {
                peer.reply(&request, json!("late")).await;
            }
            for (index, request) in current.iter().enumerate().rev() {
                peer.reply(request, json!(index)).await;
            }
            peer.eof().await;
        },
        |path| async move {
            let socket = Stream::connect(path).await.unwrap();
            let stream = QmpStreamTokio::open(socket)
                .await
                .unwrap()
                .negotiate()
                .await
                .unwrap();
            let (service, events) = stream.into_parts();
            events.release().unwrap();
            let driver = tokio::spawn(events);
            let batch = service
                .prepare_batch(
                    (0..8)
                        .map(|_| prepared(commands::query_status {}))
                        .collect(),
                )
                .unwrap();
            drop(service.submit_batch(batch).await.unwrap());
            let batch = service
                .prepare_batch(
                    (0..8)
                        .map(|_| prepared(commands::query_status {}))
                        .collect(),
                )
                .unwrap();
            let replies = service.submit_batch(batch).await.unwrap().responses().await;
            assert_eq!(
                replies
                    .into_iter()
                    .map(|reply| reply.unwrap())
                    .collect::<Vec<_>>(),
                (0..8).map(|i| json!(i)).collect::<Vec<_>>()
            );
            drop(service);
            driver.await.unwrap().unwrap();
        },
    ));
}

#[test]
fn dropping_a_finish_future_freezes_the_session_before_another_write() {
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            peer.request().await;
            peer.eof().await;
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            let batch = session
                .prepare_batch(vec![prepared(commands::query_status {})])
                .unwrap();
            let batch = session.submit_batch(batch, deadline()).await.unwrap();
            let mut finish = Box::pin(session.finish_batch(batch, deadline()));
            assert!(finish.as_mut().now_or_never().is_none());
            drop(finish);
            let error = session
                .command(commands::query_status {}, deadline())
                .await
                .unwrap_err();
            assert_eq!(error.kind, ErrorKind::State);
            assert!(error.message.contains("abandoned"));
            assert!(Arc::ptr_eq(&error, &session.close().await.unwrap_err()));
        },
    ));
}

#[test]
fn malformed_frames_and_idless_errors_preserve_the_driver_failure() {
    run(async {
        for (bytes, expected) in [
            (
                b"{\"error\":{\"class\":\"GenericError\",\"desc\":\"no id\"}}\n".to_vec(),
                "without request ID",
            ),
            (b"invalid json\n".to_vec(), "expected value"),
            (vec![b'x'; QMP_MAX_FRAME_LENGTH + 1], "exceeds limit"),
        ] {
            socket_case(
                move |mut peer| async move {
                    peer.negotiate().await;
                    peer.request().await;
                    // Once the decoder fails, the client may close during a long write.
                    let _ = peer.write.write_all(&bytes).await;
                },
                |path| async move {
                    let mut session = connect(path, deadline()).await.unwrap();
                    let error = session
                        .command(commands::query_status {}, deadline())
                        .await
                        .unwrap_err();
                    assert_eq!(error.kind, ErrorKind::Transport);
                    assert!(error.message.contains(expected), "{}", error.message);
                    assert!(Arc::ptr_eq(&error, &session.close().await.unwrap_err()));
                },
            )
            .await;
        }
    });
}

#[test]
fn connection_and_rpc_deadlines_share_the_callers_absolute_budget() {
    run(async {
        for phase in 0..3 {
            socket_case(
                move |mut peer| async move {
                    if phase >= 1 {
                        peer.greeting().await;
                        peer.request().await;
                    }
                    // phase 1 holds capability negotiation; phase 2 holds an RPC.
                    if phase == 2 {
                        peer.raw(b"{\"return\":{},\"id\":0}\n").await;
                        peer.request().await;
                    }
                    peer.eof().await;
                },
                |path| async move {
                    if phase < 2 {
                        let error =
                            match connect(path, Instant::now() + Duration::from_millis(30)).await {
                                Err(error) => error,
                                Ok(_) => panic!("missing negotiation timeout"),
                            };
                        assert_eq!(error.kind, ErrorKind::Timeout);
                    } else {
                        let mut session = connect(path, deadline()).await.unwrap();
                        let error = session
                            .command(
                                commands::query_status {},
                                Instant::now() + Duration::from_millis(30),
                            )
                            .await
                            .unwrap_err();
                        assert_eq!(error.kind, ErrorKind::Timeout);
                        assert!(Arc::ptr_eq(&error, &session.close().await.unwrap_err()));
                    }
                },
            )
            .await;
        }
    });
}

#[test]
fn dropping_a_backpressured_submit_freezes_and_closes_the_partial_write() {
    let (started, ready) = oneshot::channel();
    let (release, released) = oneshot::channel();
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            assert_eq!(peer.read.read_u8().await.unwrap(), b'{');
            started.send(()).unwrap();
            released.await.unwrap();
            let mut remainder = Vec::new();
            peer.read.read_to_end(&mut remainder).await.unwrap();
            assert!(
                remainder.len() < 8 * 1024 * 1024,
                "submission was expected to block"
            );
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            let batch = session
                .prepare_batch(
                    (0..8)
                        .map(|_| {
                            prepared(commands::qom_set {
                                path: "/machine".into(),
                                property: "test".into(),
                                value: Value::String("x".repeat(1024 * 1024)),
                            })
                        })
                        .collect(),
                )
                .unwrap();
            let mut submission = Box::pin(session.submit_batch(batch, deadline()));
            let ready = ready.fuse();
            futures::pin_mut!(ready);
            futures::select_biased! { _ = submission.as_mut().fuse() => panic!("large write did not block"), _ = ready => () }
            drop(submission);
            let error = session
                .command(commands::query_status {}, deadline())
                .await
                .unwrap_err();
            assert_eq!(error.kind, ErrorKind::State);
            release.send(()).unwrap();
            assert!(Arc::ptr_eq(&error, &session.close().await.unwrap_err()));
        },
    ));
}

#[test]
fn a_prepared_batch_cannot_write_to_another_session() {
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            peer.eof().await;
        },
        |first_path| async move {
            let mut first = connect(first_path, deadline()).await.unwrap();
            let batch = first
                .prepare_batch(vec![prepared(commands::query_status {})])
                .unwrap();
            socket_case(
                |mut peer| async move {
                    peer.negotiate().await;
                    let request = peer.request().await;
                    assert_eq!(request["execute"], "x-query-virtual-execution");
                    peer.reply(&request, json!({"current-ns":0,"state":"idle"}))
                        .await;
                    peer.eof().await;
                },
                |second_path| async move {
                    let mut second = connect(second_path, deadline()).await.unwrap();
                    let error = match second.submit_batch(batch, deadline()).await {
                        Err(error) => error,
                        Ok(_) => panic!("foreign session accepted batch"),
                    };
                    assert_eq!(error.kind, ErrorKind::Admission);
                    second
                        .command(QueryVirtualExecution {}, deadline())
                        .await
                        .unwrap();
                    second.close().await.unwrap();
                },
            )
            .await;
            first.close().await.unwrap();
        },
    ));
}

#[test]
fn a_backpressured_send_obeys_the_host_deadline() {
    let (release, released) = oneshot::channel();
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            released.await.unwrap();
            let mut bytes = Vec::new();
            peer.read.read_to_end(&mut bytes).await.unwrap();
            assert!(!bytes.is_empty() && bytes.len() < 8 * 1024 * 1024);
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            let batch = session
                .prepare_batch(
                    (0..8)
                        .map(|_| {
                            prepared(commands::qom_set {
                                path: "/machine".into(),
                                property: "test".into(),
                                value: Value::String("x".repeat(1024 * 1024)),
                            })
                        })
                        .collect(),
                )
                .unwrap();
            let error = match session
                .submit_batch(batch, Instant::now() + Duration::from_millis(30))
                .await
            {
                Err(error) => error,
                Ok(_) => panic!("send did not block"),
            };
            assert_eq!(error.kind, ErrorKind::Timeout);
            release.send(()).unwrap();
            assert!(Arc::ptr_eq(&error, &session.close().await.unwrap_err()));
        },
    ));
}

#[test]
fn cancelling_close_retains_failure_and_aborts_its_driver() {
    run(socket_case(
        |mut peer| async move {
            peer.negotiate().await;
            peer.eof().await;
        },
        |path| async move {
            let mut session = connect(path, deadline()).await.unwrap();
            let mut close = Box::pin(session.close());
            assert!(close.as_mut().now_or_never().is_none());
            drop(close);
            let error = session.close().await.unwrap_err();
            assert_eq!(error.kind, ErrorKind::State);
            assert!(error.message.contains("cleanup was interrupted"));
            assert!(error.cleanup_failed());
        },
    ));
}

#[test]
#[ignore = "requires an owned paused native QEMU with icount shift=0; set QMP_SOCKET"]
#[cfg(unix)]
fn native_qmp_consumer() {
    run(async {
        let path =
            std::env::var_os("QMP_SOCKET").expect("set QMP_SOCKET to the harness-owned QEMU");
        let host_deadline = deadline();
        let mut session = QmpSession::connect_uds(Address::from(path), host_deadline)
            .await
            .unwrap();
        let supported = session
            .command(commands::query_commands {}, host_deadline)
            .await
            .unwrap();
        for required in [
            "x-query-virtual-execution",
            "x-run-to-virtual-time",
            "qom-get",
        ] {
            assert!(
                supported.iter().any(|command| command.name == required),
                "missing {required}"
            );
        }
        // Standard 9.1-generated commands consume only fields shared with 8.2.2.
        session
            .command(commands::stop {}, host_deadline)
            .await
            .unwrap();
        assert!(
            !session
                .command(commands::query_status {}, host_deadline)
                .await
                .unwrap()
                .running
        );
        session
            .command(commands::cont {}, host_deadline)
            .await
            .unwrap();
        session
            .command(commands::stop {}, host_deadline)
            .await
            .unwrap();
        let origin = session
            .command(QueryVirtualExecution {}, host_deadline)
            .await
            .unwrap();
        let target = origin.current_ns.checked_add(1_000_000).unwrap();
        session
            .command(RunToVirtualTime { target_ns: target }, host_deadline)
            .await
            .unwrap();
        loop {
            assert!(
                Instant::now() < host_deadline,
                "native virtual target did not complete"
            );
            let values = session
                .command_many(
                    vec![
                        prepared(QueryVirtualExecution {}),
                        prepared(commands::query_status {}),
                    ],
                    host_deadline,
                )
                .await
                .unwrap();
            let execution: sim_host::qmp::VirtualExecutionInfo =
                serde_json::from_value(values[0].clone()).unwrap();
            let status: commands::StatusInfo = serde_json::from_value(values[1].clone()).unwrap();
            if execution.state == VirtualExecutionState::Reached && !status.running {
                assert_eq!(execution.current_ns, target);
                assert_eq!(execution.target_ns, Some(target));
                break;
            }
        }
        let board = session
            .command(
                commands::qom_get {
                    path: "/machine".into(),
                    property: "board-state".into(),
                },
                host_deadline,
            )
            .await
            .unwrap();
        assert_eq!(board["schema"], 1);
        assert_eq!(board["timestamp_ns"].as_i64(), Some(target));
        assert!(!board["board"].as_str().unwrap().is_empty());
        assert_eq!(board["gpio"].as_object().unwrap().len(), 8);
        session.close().await.unwrap();
    });
}
