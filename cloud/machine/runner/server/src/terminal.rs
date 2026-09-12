// File: server/src/terminal.rs
use std::io::{Read, Write};
use std::sync::Arc;
use std::time::Duration;

use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use portable_pty::{Child, CommandBuilder, MasterPty, NativePtySystem, PtySize, PtySystem, SlavePty};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::mpsc;

use crate::state::AppState;

async fn connect_with_retry(path: &str, attempts: u32, delay: Duration) -> Option<UnixStream> {
    for i in 0..attempts {
        match UnixStream::connect(path).await {
            Ok(s) => return Some(s),
            Err(_) if i + 1 < attempts => tokio::time::sleep(delay).await,
            Err(_) => return None,
        }
    }
    None
}

/// Agent-owned terminal: bridge WS <-> /tmp/codepilot_{session}.sock.
/// Protocol: 1 JSON handshake line, then raw VT100 bytes both ways.
/// If the runtime re-inits (inode swap), the UDS hits EOF; we close the WS and
/// let the client's reconnect logic grab the fresh inode.
pub async fn handle_codepilot_terminal(mut ws: WebSocket, session_id: String, state: Arc<AppState>) {
    let _conn_guard = crate::state::ConnGuard::new(&state);
    let sock_path = format!("/tmp/codepilot_{session_id}.sock");

    let stream = match connect_with_retry(&sock_path, 20, Duration::from_millis(500)).await {
        Some(s) => s,
        None => {
            tracing::warn!("Terminal socket never appeared: {sock_path}");
            let _ = ws.close().await;
            return;
        }
    };
    tracing::info!("Bridged terminal: {sock_path}");

    // --- Handshake: read until first '\n' ---
    let mut stream = stream;
    let mut acc = Vec::new();
    let mut tmp = [0u8; 4096];
    loop {
        match stream.read(&mut tmp).await {
            Ok(0) => { let _ = ws.close().await; return; }
            Ok(n) => acc.extend_from_slice(&tmp[..n]),
            Err(_) => { let _ = ws.close().await; return; }
        }
        if acc.contains(&b'\n') || acc.len() > 65536 {
            break;
        }
    }
    if let Some(pos) = acc.iter().position(|&b| b == b'\n') {
        let line = String::from_utf8_lossy(&acc[..pos]).into_owned();
        if ws.send(Message::Text(line)).await.is_err() { return; }
        if pos + 1 < acc.len() {
            // Scrollback bytes that arrived in the same read as the handshake.
            if ws.send(Message::Binary(acc[pos + 1..].to_vec())).await.is_err() { return; }
        }
    } else {
        let _ = ws.close().await;
        return;
    }

    // --- Raw byte pipe ---
    // Decouple UDS-reading from WS-sending: on the Python side, MuxServer
    // broadcasts PTY output via a *non-blocking* socket.sendall() to every
    // connected client. If our consumer (WS send) is ever momentarily slower
    // than the OS socket buffer can absorb, that raises BlockingIOError on
    // their end, which they catch as a generic OSError and silently close +
    // deregister our client — killing the live stream until the frontend's
    // reconnect timer fires again. We can't fix their non-blocking sendall(),
    // but we CAN make sure we are never the reason the buffer backs up: read
    // the UDS side on its own task into a bounded channel and let a separate
    // task drain that into the WebSocket, so a slow WS write never leaves the
    // UDS socket unread for longer than a channel-buffer's worth of time.
    let (mut uds_rd, mut uds_wr) = stream.into_split();
    let (mut ws_sink, mut ws_stream) = ws.split();
    let (out_tx, mut out_rx) = mpsc::channel::<Vec<u8>>(256);

    let reader_task = tokio::spawn(async move {
        let mut uds_buf = [0u8; 16384];
        loop {
            match uds_rd.read(&mut uds_buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if out_tx.send(uds_buf[..n].to_vec()).await.is_err() {
                        break;
                    }
                }
            }
        }
    });

    loop {
        tokio::select! {
            out = out_rx.recv() => match out {
                Some(bytes) => {
                    if ws_sink.send(Message::Binary(bytes)).await.is_err() { break; }
                }
                None => break, // reader_task ended: UDS closed (likely the non-blocking-sendall disconnect above)
            },
            inc = ws_stream.next() => match inc {
                Some(Ok(Message::Binary(b))) => {
                    if uds_wr.write_all(&b).await.is_err() { break; }
                }
                Some(Ok(Message::Text(t))) => {
                    // Resize requests are forwarded to the control plane.
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) {
                        if v.get("cols").is_some() || v.get("rows").is_some() {
                            let evt = serde_json::json!({
                                "event": "resize",
                                "session_id": session_id,
                                "cols": v.get("cols"),
                                "rows": v.get("rows"),
                            });
                            let mut line = evt.to_string();
                            line.push('\n');
                            let _ = state.control_plane_tx.send(line.into_bytes());
                        }
                    }
                }
                Some(Ok(Message::Ping(p))) => { let _ = ws_sink.send(Message::Pong(p)).await; }
                Some(Ok(_)) => {}
                Some(Err(_)) | None => break,
            },
        }
    }
    reader_task.abort();
    tracing::info!("Terminal session closed: {session_id}");
}

/// Private user terminal: Rust-owned PTY via portable-pty. The agent never sees it.
/// PTY I/O runs on two OS threads (blocking reads/writes) so the Tokio runtime
/// is never blocked; bytes cross via unbounded channels.
pub async fn handle_user_terminal(ws: WebSocket, state: Arc<AppState>) {
    let _conn_guard = crate::state::ConnGuard::new(&state);
    let pty_system = NativePtySystem::default();
    let pair = match pty_system.openpty(PtySize { rows: 24, cols: 80, pixel_width: 0, pixel_height: 0 }) {
        Ok(p) => p,
        Err(e) => { tracing::error!("openpty failed: {e}"); return; }
    };

    let mut cmd = CommandBuilder::new("bash");
    cmd.env_remove("MACHINE_SECRET");
    cmd.env_remove("CONTROL_PLANE_URL");
    cmd.env_remove("DEEPSEEK_API_KEY");
    cmd.env_remove("DASHSCOPE_API_KEY");
    cmd.env_remove("ALIBABA_API_KEY");
    cmd.env_remove("VOYAGE_API_KEY");
    cmd.env_remove("TAVILY_API_KEY");
    cmd.env_remove("EXPERIENTIAL_API_KEY");
    cmd.env("TERM", "xterm-256color");
    cmd.cwd(state.config.workspace_path.clone());

    let mut child = match pair.slave.spawn_command(cmd) {
        Ok(c) => c,
        Err(e) => { tracing::error!("spawn bash failed: {e}"); return; }
    };
    drop(pair.slave);

    let mut master = pair.master;
    let mut reader = match master.try_clone_reader() {
        Ok(r) => r,
        Err(e) => { tracing::error!("pty reader failed: {e}"); return; }
    };
    let mut writer = match master.take_writer() {
        Ok(w) => w,
        Err(e) => { tracing::error!("pty writer failed: {e}"); return; }
    };

    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Vec<u8>>();
    let (in_tx, mut in_rx) = mpsc::unbounded_channel::<Vec<u8>>();

    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if out_tx.send(buf[..n].to_vec()).is_err() { break; }
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
    });

    std::thread::spawn(move || {
        while let Some(data) = in_rx.blocking_recv() {
            if writer.write_all(&data).is_err() { break; }
            let _ = writer.flush();
        }
    });

    let (mut ws_sink, mut ws_stream) = ws.split();
    tracing::info!("User PTY terminal opened");

    loop {
        tokio::select! {
            out = out_rx.recv() => match out {
                Some(bytes) => {
                    if ws_sink.send(Message::Binary(bytes)).await.is_err() { break; }
                }
                None => { tracing::info!("PTY exited (bash closed)"); break; } // EOF: bash exited
            },
            inc = ws_stream.next() => match inc {
                Some(Ok(Message::Binary(b))) => { let _ = in_tx.send(b); }
                Some(Ok(Message::Text(t))) => {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) {
                        if let (Some(c), Some(r)) = (
                            v.get("cols").and_then(|x| x.as_u64()),
                            v.get("rows").and_then(|x| x.as_u64()),
                        ) {
                            let _ = master.resize(PtySize {
                                rows: r as u16,
                                cols: c as u16,
                                pixel_width: 0,
                                pixel_height: 0,
                            });
                        }
                    }
                }
                Some(Ok(Message::Ping(p))) => { let _ = ws_sink.send(Message::Pong(p)).await; }
                Some(Ok(_)) => {}
                Some(Err(_)) | None => break,
            },
        }
    }
    let _ = child.kill();
    tracing::info!("User PTY terminal closed");
}

/// Persistent listener on /tmp/codepilot_control.sock.
/// - Replays/broadcasts `terminal_created` to all /ws/control clients.
/// - Caches `terminal_created` in `state.known_terminals` for late-connecting browsers.
/// - Drains `control_plane_tx` (resize events) into the socket.
/// - On EOF (runtime re-init / inode swap) reconnects after 2s.
pub fn start_control_plane_listener(state: Arc<AppState>, mut outbound_rx: mpsc::UnboundedReceiver<Vec<u8>>) {
    let sock = state.config.codepilot_control_sock.clone();
    let clients = state.control_clients.clone();
    let known_terminals = state.known_terminals.clone();

    tokio::spawn(async move {
        loop {
            match UnixStream::connect(&sock).await {
                Ok(stream) => {
                    tracing::info!("Connected to codepilot control plane");
                    // Clear cached terminals on reconnect — the runtime re-inits its sessions
                    known_terminals.clear();

                    let (mut rd, mut wr) = stream.into_split();
                    let mut acc = Vec::new();
                    let mut buf = [0u8; 4096];

                    loop {
                        tokio::select! {
                            n = rd.read(&mut buf) => match n {
                                Ok(0) | Err(_) => break,
                                Ok(n) => {
                                    acc.extend_from_slice(&buf[..n]);
                                    while let Some(pos) = acc.iter().position(|&b| b == b'\n') {
                                        let line = String::from_utf8_lossy(&acc[..pos]).into_owned();
                                        acc.drain(..=pos);
                                        if line.contains("\"terminal_created\"") {
                                            // Cache the event for late-connecting browsers
                                            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) {
                                                if let Some(sid) = v.get("session_id").and_then(|s| s.as_str()) {
                                                    known_terminals.insert(sid.to_string(), line.clone());
                                                }
                                            }
                                            let msg = Message::Text(line);
                                            for c in clients.iter() {
                                                let _ = c.value().send(msg.clone());
                                            }
                                        }
                                    }
                                }
                            },
                            out = outbound_rx.recv() => match out {
                                Some(bytes) => { if wr.write_all(&bytes).await.is_err() { break; } }
                                None => break,
                            },
                        }
                    }
                    tracing::warn!("Control plane connection lost; will reconnect");
                }
                Err(_) => {}
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    });
}