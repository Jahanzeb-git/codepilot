// File: server/src/agent.rs
use std::sync::Arc;

use axum::extract::ws::Message;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::mpsc;

use crate::state::AppState;

/// Single persistent client of /tmp/agent_runtime.sock (it only accepts one).
/// - UDS -> fan out raw NDJSON bytes to every /ws/agent browser client.
/// - Browser NDJSON (via agent_inbound_tx) -> UDS.
/// - On EOF / error: reconnect after 2s (covers runtime re-init inode swaps).
pub async fn run_agent_bridge(state: Arc<AppState>, mut inbound_rx: mpsc::UnboundedReceiver<Vec<u8>>) {
    let sock = state.config.agent_runtime_sock.clone();
    let clients = state.agent_clients.clone();

    loop {
        match UnixStream::connect(&sock).await {
            Ok(stream) => {
                tracing::info!("Agent bridge connected to {}", sock.display());
                let (mut rd, mut wr) = stream.into_split();
                let mut buf = [0u8; 16384];

                loop {
                    tokio::select! {
                        n = rd.read(&mut buf) => match n {
                            Ok(0) | Err(_) => break,
                            Ok(n) => {
                                let msg = Message::Binary(buf[..n].to_vec());
                                for c in clients.iter() {
                                    let _ = c.value().send(msg.clone());
                                }
                            }
                        },
                        out = inbound_rx.recv() => match out {
                            Some(bytes) => { if wr.write_all(&bytes).await.is_err() { break; } }
                            None => break,
                        },
                    }
                }
                tracing::warn!("Agent bridge disconnected; reconnecting");
            }
            Err(e) => {
                tracing::warn!("Agent socket not ready ({e}); retrying in 2s");
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
}