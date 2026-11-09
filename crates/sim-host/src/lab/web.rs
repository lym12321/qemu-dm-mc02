//! Loopback HTTP/WebSocket adapter; the session thread owns native execution.
use super::session::{LabSession, SessionError, SessionOptions};
use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        DefaultBodyLimit, Multipart, Request, State,
    },
    http::{header, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use futures::{stream::FuturesUnordered, FutureExt, SinkExt, StreamExt};
use serde_json::{json, Map, Value};
use sim_core::Error;
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::{io::AsyncWriteExt, net::TcpListener};
use uuid::Uuid;

const UPLOAD_LIMIT: usize = 64 * 1024 * 1024;
const SEND_TIMEOUT: Duration = Duration::from_secs(2);

struct HttpError(StatusCode, String);
impl IntoResponse for HttpError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}
fn bad(error: impl std::fmt::Display) -> HttpError {
    HttpError(StatusCode::BAD_REQUEST, error.to_string())
}
fn internal(error: impl std::fmt::Display) -> HttpError {
    HttpError(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
}
fn failed(error: impl std::error::Error + Send + Sync + 'static) -> SessionError {
    SessionError {
        primary: Arc::new(error),
        notes: vec![],
        input: false,
    }
}
#[derive(Default)]
struct Resources {
    controller: Option<String>,
    upload: Option<tempfile::TempDir>,
}
struct WebState {
    session: Arc<LabSession>,
    resources: Mutex<Resources>,
    upload_lock: tokio::sync::Mutex<()>,
    cancelled: Arc<AtomicBool>,
}
fn is_active(value: &Value) -> bool {
    matches!(
        value["status"].as_str(),
        Some("starting" | "paused" | "running" | "stopping")
    )
}

async fn local_origin(request: Request, next: Next) -> Response {
    let upload = request.uri().path() == "/api/firmware";
    let permitted = (|| {
        let host = request
            .headers()
            .get(header::HOST)
            .and_then(|value| value.to_str().ok())
            .ok_or("local Host required")?;
        let authority: axum::http::uri::Authority =
            host.parse().map_err(|_| "local Host required")?;
        if !authority.host().eq_ignore_ascii_case("localhost") && authority.host() != "127.0.0.1" {
            return Err("local Host required");
        }
        if request
            .headers()
            .get(header::ORIGIN)
            .is_some_and(|origin| origin != format!("http://{host}").as_str())
        {
            return Err("same-origin control required");
        }
        Ok(())
    })();
    let mut response = match permitted {
        Ok(()) => next.run(request).await,
        Err(error) => HttpError(StatusCode::FORBIDDEN, error.into()).into_response(),
    };
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response.headers_mut().insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    // Rejected uploads may retain an unread multipart suffix. Advertise close
    // so the client cannot reuse that connection for its next firmware request.
    if upload && !response.status().is_success() {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
    }
    response
}
async fn state(State(state): State<Arc<WebState>>) -> Json<Value> {
    Json(state.session.state())
}
async fn index() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        include_bytes!("../../../../sim/lab/static/index.html").as_slice(),
    )
}
async fn javascript() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/javascript; charset=utf-8")],
        include_bytes!("../../../../sim/lab/static/app.js").as_slice(),
    )
}
async fn stylesheet() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_bytes!("../../../../sim/lab/static/style.css").as_slice(),
    )
}

async fn upload(
    State(state): State<Arc<WebState>>,
    multipart: std::result::Result<Multipart, axum::extract::multipart::MultipartRejection>,
) -> std::result::Result<Json<Value>, HttpError> {
    let _guard = state.upload_lock.lock().await;
    if is_active(&state.session.state()) {
        return Err(HttpError(
            StatusCode::CONFLICT,
            "stop the current experiment before uploading firmware".into(),
        ));
    }
    let mut multipart = multipart.map_err(bad)?;
    let temporary = tempfile::Builder::new()
        .prefix("dm-web-firmware.")
        .tempdir()
        .map_err(internal)?;
    let path = temporary.path().join("firmware.img");
    let mut field = multipart
        .next_field()
        .await
        .map_err(bad)?
        .ok_or_else(|| bad("one firmware file is required"))?;
    if field.name() != Some("firmware") {
        return Err(bad("one firmware file is required"));
    }
    let filename = field
        .file_name()
        .ok_or_else(|| bad("one firmware file is required"))?
        .replace('\\', "/");
    let name = filename.rsplit('/').next().unwrap();
    if name.is_empty() || name.chars().count() > 255 || name.chars().any(|ch| ch < ' ') {
        return Err(bad("invalid firmware filename"));
    }
    let mut file = tokio::fs::File::create(&path).await.map_err(internal)?;
    let mut size = 0;
    while let Some(chunk) = field
        .chunk()
        .await
        .map_err(|error| HttpError(error.status(), error.to_string()))?
    {
        size += chunk.len();
        if size > UPLOAD_LIMIT {
            return Err(HttpError(
                StatusCode::PAYLOAD_TOO_LARGE,
                "firmware exceeds64MiB".into(),
            ));
        }
        for chunk in chunk.chunks(65536) {
            file.write_all(chunk).await.map_err(internal)?;
        }
    }
    drop(field);
    file.flush().await.map_err(internal)?;
    drop(file);
    if size == 0 || multipart.next_field().await.map_err(bad)?.is_some() {
        return Err(bad("exactly one nonempty firmware file is required"));
    }
    let value = state
        .session
        .select_firmware(path, Some(name))
        .map_err(bad)?;
    let previous = state.resources.lock().unwrap().upload.replace(temporary);
    if let Some(previous) = previous {
        previous.close().map_err(internal)?;
    }
    Ok(Json(value))
}

#[derive(Debug)]
struct Envelope {
    id: String,
    session_id: Option<String>,
    action: String,
    args: Map<String, Value>,
}
fn envelope(text: &str) -> std::result::Result<Envelope, String> {
    let mut value: Map<String, Value> =
        serde_json::from_str(text).map_err(|error| error.to_string())?;
    if value.len() != 4
        || ["id", "session_id", "action", "args"]
            .iter()
            .any(|key| !value.contains_key(*key))
    {
        return Err("expected id, session_id, action and args".into());
    }
    let id = value
        .remove("id")
        .unwrap()
        .as_str()
        .filter(|id| (1..=128).contains(&id.chars().count()))
        .ok_or("id must be a nonempty string of at most128 characters")?
        .to_owned();
    let session_id = match value.remove("session_id").unwrap() {
        Value::Null => None,
        Value::String(id) if id.chars().count() <= 64 => Some(id),
        _ => return Err("invalid session_id".into()),
    };
    let action = value
        .remove("action")
        .unwrap()
        .as_str()
        .ok_or("action must be a string")?
        .to_owned();
    let Value::Object(args) = value.remove("args").unwrap() else {
        return Err("args must be an object".into());
    };
    Ok(Envelope {
        id,
        session_id,
        action,
        args,
    })
}
struct ReplyError {
    message: String,
    notes: Vec<String>,
}
impl From<SessionError> for ReplyError {
    fn from(error: SessionError) -> Self {
        Self {
            message: error.to_string(),
            notes: error.notes,
        }
    }
}
impl From<futures::channel::oneshot::Canceled> for ReplyError {
    fn from(error: futures::channel::oneshot::Canceled) -> Self {
        Self {
            message: error.to_string(),
            notes: vec![],
        }
    }
}
impl From<&str> for ReplyError {
    fn from(message: &str) -> Self {
        Self {
            message: message.into(),
            notes: vec![],
        }
    }
}
async fn command(state: Arc<WebState>, controller: String, request: Envelope) -> (Value, bool) {
    let is_stop = request.action == "stop";
    let result: std::result::Result<(), ReplyError> = async {
        if matches!(request.action.as_str(), "stop" | "restart") {
            if !request.args.is_empty()
                || state.session.state()["session_id"] != json!(request.session_id)
            {
                return Err("invalid lifecycle request or stale session".into());
            }
            state
                .session
                .request_stop(request.session_id.as_deref())
                .await??;
            if request.action == "restart" {
                state
                    .session
                    .submit(
                        "load",
                        json!({}),
                        request.session_id.as_deref(),
                        Some(&controller),
                    )
                    .await??;
            }
        } else {
            state
                .session
                .submit(
                    &request.action,
                    Value::Object(request.args),
                    request.session_id.as_deref(),
                    Some(&controller),
                )
                .await??;
        }
        Ok(())
    }
    .await;
    let response = match result {
        Ok(()) => {
            json!({"type":"command_result","id":request.id,"session_id":state.session.state()["session_id"],"ok":true,"accepted":true})
        }
        Err(error) => {
            json!({"type":"command_result","id":request.id,"session_id":request.session_id,"ok":false,"accepted":false,"error":error.message,"notes":error.notes})
        }
    };
    (response, is_stop)
}
struct Controller {
    state: Arc<WebState>,
    id: String,
}
impl Drop for Controller {
    fn drop(&mut self) {
        self.state.session.disconnect(&self.id);
        let mut resources = self.state.resources.lock().unwrap();
        if resources.controller.as_deref() == Some(&self.id) {
            resources.controller = None;
        }
    }
}
async fn websocket(
    State(state): State<Arc<WebState>>,
    upgrade: WebSocketUpgrade,
) -> std::result::Result<Response, HttpError> {
    let controller = Uuid::new_v4().simple().to_string();
    {
        let mut resources = state.resources.lock().unwrap();
        if resources.controller.is_some() {
            return Err(HttpError(
                StatusCode::CONFLICT,
                "another page already controls this workbench".into(),
            ));
        }
        resources.controller = Some(controller.clone());
    }
    let guard = Controller {
        state,
        id: controller,
    };
    // The same guard also releases the claim when the native upgrade fails.
    let guard = Arc::new(Mutex::new(Some(guard)));
    let failed = guard.clone();
    Ok(upgrade
        .max_message_size(16384)
        .max_frame_size(16384)
        .on_failed_upgrade(move |_| {
            failed.lock().unwrap().take();
        })
        .on_upgrade(move |socket| async move {
            let controller = guard.lock().unwrap().take().unwrap();
            socket_loop(socket, controller).await;
        }))
}
async fn send(sink: &mut futures::stream::SplitSink<WebSocket, Message>, message: Message) -> bool {
    matches!(
        tokio::time::timeout(SEND_TIMEOUT, sink.send(message)).await,
        Ok(Ok(()))
    )
}
async fn send_json(
    sink: &mut futures::stream::SplitSink<WebSocket, Message>,
    value: Value,
) -> bool {
    send(sink, Message::Text(value.to_string().into())).await
}
async fn socket_loop(socket: WebSocket, controller: Controller) {
    let state = controller.state.clone();
    let (mut sink, mut stream) = socket.split();
    let mut pending = FuturesUnordered::new();
    let mut stop_pending = false;
    let mut ticker = tokio::time::interval(Duration::from_millis(100));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut after = 0;
    let mut last_message = Instant::now();
    let mut ping_sent = None;
    loop {
        if state.cancelled.load(Ordering::Relaxed) {
            break;
        }
        tokio::select! {
            message = stream.next() => {
                let Some(Ok(message)) = message else { break; };
                last_message = Instant::now();
                match message {
                    Message::Close(_) => break,
                    Message::Pong(_) => ping_sent = None,
                    Message::Text(text) => {
                        let request = envelope(&text).and_then(|request| {
                            if pending.len() >=64 && (request.action != "stop" || stop_pending) { Err("too many pending requests".into()) } else { Ok(request) }
                        });
                        match request {
                            Ok(request) => {
                                stop_pending |= request.action == "stop";
                                pending.push(command(state.clone(),controller.id.clone(),request).boxed());
                            }
                            Err(error) => if !send_json(&mut sink,json!({"type":"command_result","id":null,"ok":false,"accepted":false,"error":error})).await { break; },
                        }
                    }
                    _ => (),
                }
            }
            Some((response,is_stop)) = pending.next(), if !pending.is_empty() => {
                if is_stop { stop_pending = false; }
                if !send_json(&mut sink,response).await || !send_json(&mut sink,state.session.state()).await { break; }
            }
            _ = ticker.tick() => {
                if ping_sent.is_some_and(|at:Instant| at.elapsed() > Duration::from_millis(7500)) { break; }
                if ping_sent.is_none() && last_message.elapsed() >= Duration::from_secs(15) {
                    if !send(&mut sink,Message::Ping(Vec::new().into())).await { break; }
                    ping_sent = Some(Instant::now());
                }
                if !send_json(&mut sink,state.session.state()).await { break; }
                for event in state.session.history(after) {
                    after = event["seq"].as_str().unwrap().parse().unwrap();
                    if state.cancelled.load(Ordering::Relaxed) || !send_json(&mut sink,event).await { return; }
                }
            }
        }
    }
    drop(pending);
    drop(controller); // Release momentary inputs before any closing socket IO.
    let _ = send(&mut sink, Message::Close(None)).await;
}

/// Own the server and uploaded firmware until session/thread cleanup completes.
/// Signals only set the shared request; all native cleanup stays with the owner.
pub fn run_web(
    options: SessionOptions,
    port: u16,
    cancelled: Arc<AtomicBool>,
) -> std::result::Result<(), SessionError> {
    if port == 0 {
        return Err(failed(Error::Admission(
            "HTTP port must be in1..65535".into(),
        )));
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(failed)?;
    let session = Arc::new(LabSession::new(options)?);
    let state = Arc::new(WebState {
        session: session.clone(),
        resources: Mutex::new(Resources::default()),
        upload_lock: tokio::sync::Mutex::new(()),
        cancelled: cancelled.clone(),
    });
    let router = Router::new()
        .route("/", get(index))
        .route("/app.js", get(javascript))
        .route("/style.css", get(stylesheet))
        .route("/api/state", get(self::state))
        .route("/api/firmware", post(upload))
        .route("/api/ws", get(websocket))
        .layer(DefaultBodyLimit::max(UPLOAD_LIMIT + 65536))
        .layer(middleware::from_fn(local_origin))
        .with_state(state.clone());
    let execution: std::result::Result<(), SessionError> = runtime.block_on(async {
        let listener = TcpListener::bind(("127.0.0.1", port))
            .await
            .map_err(failed)?;
        println!("Web Lab: http://127.0.0.1:{port} (Ctrl-C to stop)");
        axum::serve(listener, router)
            .with_graceful_shutdown(async move {
                while !cancelled.load(Ordering::Relaxed) {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            })
            .await
            .map_err(failed)?;
        Err(failed(Error::Interrupted("Web Lab interrupted".into())))
    });
    let mut primary = execution.err();
    if let Err(error) = session.close() {
        if let Some(primary) = &mut primary {
            primary.notes.push(format!("Web Lab cleanup: {error}"));
            primary.notes.extend(error.notes);
        } else {
            primary = Some(error);
        }
    }
    let upload = state.resources.lock().unwrap().upload.take();
    if let Some(upload) = upload {
        if let Err(error) = upload.close() {
            if let Some(primary) = &mut primary {
                primary
                    .notes
                    .push(format!("uploaded firmware cleanup: {error}"));
            } else {
                primary = Some(failed(error));
            }
        }
    }
    match primary {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::envelope;

    #[test]
    fn envelope_requires_exact_fields_and_keeps_unicode_ids_and_object_args() {
        let value = envelope(
            r#"{"id":"请求","session_id":null,"action":"uart","args":{"data_b64":"AP8="}}"#,
        )
        .unwrap();
        assert_eq!(value.id, "请求");
        assert_eq!(value.args["data_b64"], "AP8=");
        for text in [
            r#"[]"#,
            r#"{"id":"x","action":"stop","args":{}}"#,
            r#"{"id":"x","session_id":null,"action":"stop","args":[],"extra":1}"#,
            r#"{"id":0,"session_id":null,"action":"stop","args":{}}"#,
        ] {
            assert!(envelope(text).is_err(), "{text}");
        }
    }
}
