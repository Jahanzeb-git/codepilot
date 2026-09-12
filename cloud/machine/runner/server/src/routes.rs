// File: server/src/routes.rs
use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Query, State};
use axum::response::{Html, IntoResponse};
use axum::routing::get;
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use uuid::Uuid;

use crate::state::AppState;
use crate::{fs, terminal};

#[derive(Deserialize)]
pub struct TerminalQuery {
    pub session: Option<String>,
}

/// Placeholder page served until the real React frontend exists.
/// Doubles as a zero-setup smoke-test client for all three WebSockets.
const PLACEHOLDER_HTML: &str = r#"<!doctype html>
<html><head><title>codepilot-server</title></head>
<body style="font-family:monospace;background:#101014;color:#7CFC9A;padding:2rem">
<h2>codepilot-server is running</h2>
<p>Frontend not deployed yet. Live smoke-test of the three WebSocket planes:</p>
<pre id="log" style="background:#000;padding:1rem;height:70vh;overflow:auto"></pre>
<script>
var log=function(m){var el=document.getElementById('log');el.textContent+=m+'\n';el.scrollTop=el.scrollHeight;};
var proto=location.protocol==='https:'?'wss:':'ws:';
var c=new WebSocket(proto+'//'+location.host+'/ws/control');
c.onopen=function(){log('[control] connected -> list_dir /workspace');c.send(JSON.stringify({id:'1',type:'list_dir',path:'/workspace'}));};
c.onmessage=function(e){log('[control] '+e.data);};
c.onclose=function(){log('[control] closed');};
var t=new WebSocket(proto+'//'+location.host+'/ws/terminal?session=smoke_'+Date.now());
t.binaryType='arraybuffer';
t.onopen=function(){log('[terminal] connected (private Rust PTY)');};
t.onmessage=function(e){
  if(typeof e.data==='string'){log('[terminal handshake] '+e.data);return;}
  log(new TextDecoder().decode(e.data).replace(/\x1b\[[0-9;]*[A-Za-z]/g,''));
};
var a=new WebSocket(proto+'//'+location.host+'/ws/agent');
a.onopen=function(){log('[agent] connected');};
a.onmessage=function(e){log('[agent] '+(typeof e.data==='string'?e.data:new TextDecoder().decode(e.data)));};
</script></body></html>"#;

pub async fn index_handler(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let idx = state.config.static_path.join("index.html");
    match tokio::fs::read(&idx).await {
        Ok(bytes) => axum::http::Response::builder()
            .header("content-type", "text/html; charset=utf-8")
            .body(axum::body::Body::from(bytes))
            .unwrap()
            .into_response(),
        Err(_) => Html(PLACEHOLDER_HTML).into_response(),
    }
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/", get(index_handler))
        .route("/ws/control", get(ws_control_handler))
        .route("/ws/terminal", get(ws_terminal_handler))
        .route("/ws/agent", get(ws_agent_handler))
        .fallback_service(tower_http::services::ServeDir::new(
            state.config.static_path.clone(),
        ))
        .with_state(state)
}

// ---------------------------------------------------------------- /ws/control

pub async fn ws_control_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_control_socket(socket, state))
}

async fn handle_control_socket(ws: WebSocket, state: Arc<AppState>) {
    let _conn_guard = crate::state::ConnGuard::new(&state);
    let id = Uuid::new_v4();
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    state.control_clients.insert(id, tx.clone());

    // Replay cached terminal_created events to this newly connected client
    // so it can connect to agent terminals that were created before the browser loaded.
    for entry in state.known_terminals.iter() {
        let _ = tx.send(Message::Text(entry.value().clone()));
    }

    // Outbound pump: internal messages -> browser.
    let (mut ws_sink, mut ws_stream) = ws.split();
    let send_task = tokio::spawn(async move {
        while let Some(msg) = rx.recv().await {
            if ws_sink.send(msg).await.is_err() {
                break;
            }
        }
    });

    // Watcher pump: inotify broadcasts -> this client.
    let mut fs_rx = state.fs_events_tx.subscribe();
    let watch_tx = tx.clone();
    let watch_task = tokio::spawn(async move {
        while let Ok(ev) = fs_rx.recv().await {
            let t = match ev.event_type.as_str() {
                "created" => "file_created",
                "deleted" => "file_deleted",
                _ => "file_changed",
            };
            let msg = serde_json::json!({ "type": t, "path": ev.path });
            if watch_tx.send(Message::Text(msg.to_string())).is_err() {
                break;
            }
        }
    });

    // Inbound: JSON requests -> fs ops.
    while let Some(Ok(msg)) = ws_stream.next().await {
        let Message::Text(text) = msg else { continue };
        let Ok(req) = serde_json::from_str::<serde_json::Value>(&text) else { continue };

        let req_id = req.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let req_type = req.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let path_str = req.get("path").and_then(|v| v.as_str()).unwrap_or("");

        if req_type == "ping" {
            let _ = tx.send(Message::Text(
                serde_json::json!({ "id": req_id, "type": "pong" }).to_string(),
            ));
            continue;
        }

        let resp = match fs::validate_path(path_str, &state.config.workspace_path) {
            Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
            Ok(path) => match req_type {
                "read_file" => match fs::read_file(&path).await {
                    Ok(content) => serde_json::json!({ "id": req_id, "type": "file_content", "path": path_str, "content": content, "success": true }),
                    Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                },
                "write_file" => {
                    let content = req.get("content").and_then(|v| v.as_str()).unwrap_or("");
                    match fs::write_file(&path, content).await {
                        Ok(()) => serde_json::json!({ "id": req_id, "type": "operation_result", "success": true }),
                        Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                    }
                }
                "create_file" => match fs::create_file(&path).await {
                    Ok(()) => serde_json::json!({ "id": req_id, "type": "operation_result", "success": true }),
                    Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                },
                "delete_file" => match fs::delete_file(&path).await {
                    Ok(()) => serde_json::json!({ "id": req_id, "type": "operation_result", "success": true }),
                    Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                },
                "create_dir" => match fs::create_dir(&path).await {
                    Ok(()) => serde_json::json!({ "id": req_id, "type": "operation_result", "success": true }),
                    Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                },
                "delete_dir" => match fs::delete_dir(&path).await {
                    Ok(()) => serde_json::json!({ "id": req_id, "type": "operation_result", "success": true }),
                    Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                },
                "list_dir" => match fs::list_dir(&path).await {
                    Ok(entries) => serde_json::json!({ "id": req_id, "type": "dir_list", "path": path_str, "data": entries, "success": true }),
                    Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                },
                "git_clone" => {
                    let url = req.get("url").and_then(|v| v.as_str()).unwrap_or("");
                    if url.is_empty() {
                        serde_json::json!({ "id": req_id, "type": "error", "error": "Empty URL", "success": false })
                    } else {
                        // `git clone <url> .` fails whenever the workspace dir
                        // already contains anything (even just a mounted volume
                        // placeholder), since git refuses to clone into a
                        // non-empty directory. Clone into a scratch dir instead,
                        // then move its contents (including dotfiles like .git)
                        // into the workspace root, overwriting any name clashes.
                        let wp = state.config.workspace_path.clone();
                        let tmp = wp.join(format!(".git_clone_tmp_{}", std::process::id()));
                        let _ = tokio::fs::remove_dir_all(&tmp).await;

                        let clone_status = tokio::process::Command::new("git")
                            .arg("clone")
                            .arg(url)
                            .arg(&tmp)
                            .status()
                            .await;

                        let result = match clone_status {
                            Ok(s) if s.success() => match tokio::fs::read_dir(&tmp).await {
                                Ok(mut rd) => {
                                    let mut moved = Ok(());
                                    loop {
                                        match rd.next_entry().await {
                                            Ok(Some(entry)) => {
                                                let dest = wp.join(entry.file_name());
                                                if dest.exists() {
                                                    let is_dir = dest.is_dir();
                                                    let rm = if is_dir {
                                                        tokio::fs::remove_dir_all(&dest).await
                                                    } else {
                                                        tokio::fs::remove_file(&dest).await
                                                    };
                                                    if let Err(e) = rm {
                                                        moved = Err(e.to_string());
                                                        break;
                                                    }
                                                }
                                                if let Err(e) = tokio::fs::rename(entry.path(), &dest).await {
                                                    moved = Err(e.to_string());
                                                    break;
                                                }
                                            }
                                            Ok(None) => break,
                                            Err(e) => {
                                                moved = Err(e.to_string());
                                                break;
                                            }
                                        }
                                    }
                                    match moved {
                                        Ok(()) => serde_json::json!({ "id": req_id, "type": "operation_result", "success": true }),
                                        Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": format!("failed to move cloned files: {e}"), "success": false }),
                                    }
                                }
                                Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                            },
                            Ok(s) => serde_json::json!({ "id": req_id, "type": "error", "error": format!("git clone failed with status {}", s), "success": false }),
                            Err(e) => serde_json::json!({ "id": req_id, "type": "error", "error": e.to_string(), "success": false }),
                        };

                        let _ = tokio::fs::remove_dir_all(&tmp).await;
                        result
                    }
                },
                _ => serde_json::json!({ "id": req_id, "type": "error", "error": format!("unknown type: {req_type}"), "success": false }),
            },
        };
        let _ = tx.send(Message::Text(resp.to_string()));
    }

    state.control_clients.remove(&id);
    send_task.abort();
    watch_task.abort();
}

// ---------------------------------------------------------------- /ws/terminal

pub async fn ws_terminal_handler(
    ws: WebSocketUpgrade,
    Query(q): Query<TerminalQuery>,
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
    let session = q.session.unwrap_or_else(|| "main".to_string());
    ws.on_upgrade(move |socket| async move {
        let sock = format!("/tmp/codepilot_{session}.sock");
        // Route by reality, not by name: if the codepilot runtime owns this
        // session socket, bridge to it; otherwise spawn a private Rust PTY.
        if tokio::fs::metadata(&sock).await.is_ok() {
            terminal::handle_codepilot_terminal(socket, session, state).await;
        } else {
            terminal::handle_user_terminal(socket, state).await;
        }
    })
}

// ---------------------------------------------------------------- /ws/agent

pub async fn ws_agent_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_agent_socket(socket, state))
}

async fn handle_agent_socket(ws: WebSocket, state: Arc<AppState>) {
    let _conn_guard = crate::state::ConnGuard::new(&state);
    let id = Uuid::new_v4();
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    state.agent_clients.insert(id, tx);

    let (mut ws_sink, mut ws_stream) = ws.split();
    loop {
        tokio::select! {
            out = rx.recv() => match out {
                Some(msg) => { if ws_sink.send(msg).await.is_err() { break; } }
                None => break,
            },
            inc = ws_stream.next() => match inc {
                Some(Ok(Message::Text(t))) => {
                    let mut bytes = t.into_bytes();
                    if !bytes.ends_with(b"\n") { bytes.push(b'\n'); }
                    let _ = state.agent_inbound_tx.send(bytes);
                }
                Some(Ok(Message::Binary(b))) => { let _ = state.agent_inbound_tx.send(b); }
                Some(Ok(Message::Ping(p))) => { let _ = ws_sink.send(Message::Pong(p)).await; }
                Some(Ok(_)) => {}
                Some(Err(_)) | None => break,
            },
        }
    }
    state.agent_clients.remove(&id);
}