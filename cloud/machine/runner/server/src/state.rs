// File: server/src/state.rs
use std::sync::Arc;
use std::sync::atomic::AtomicI64;

use axum::extract::ws::Message;
use dashmap::DashMap;
use tokio::sync::{broadcast, mpsc};
use uuid::Uuid;

#[derive(Clone, Debug, serde::Serialize)]
pub struct FsEvent {
    /// One of "created" | "modified" | "deleted"
    pub event_type: String,
    pub path: String,
}

pub struct AppState {
    pub config: Arc<crate::config::Config>,
    /// Broadcasts inotify events to every /ws/control client.
    pub fs_events_tx: broadcast::Sender<FsEvent>,
    /// Live /ws/control connections (for server-pushed events like terminal_created).
    pub control_clients: Arc<DashMap<Uuid, mpsc::UnboundedSender<Message>>>,
    /// Live /ws/agent connections (fan-out of agent NDJSON).
    pub agent_clients: Arc<DashMap<Uuid, mpsc::UnboundedSender<Message>>>,
    /// Browser→Python NDJSON bytes, consumed by the agent bridge task.
    pub agent_inbound_tx: mpsc::UnboundedSender<Vec<u8>>,
    /// Bytes to write to /tmp/codepilot_control.sock (resize events), consumed by the control-plane task.
    pub control_plane_tx: mpsc::UnboundedSender<Vec<u8>>,
    /// Cached `terminal_created` events (keyed by session_id → full JSON string).
    /// Replayed to every new /ws/control client so agent terminals are never missed
    /// due to the browser connecting after the codepilot runtime already started.
    pub known_terminals: Arc<DashMap<String, String>>,
    /// Count of every currently-live browser-facing WebSocket this machine is
    /// serving — /ws/control, /ws/agent, and every /ws/terminal/* connection
    /// (proxied through codepilot-api, but terminated here). This is the
    /// machine's own view of "is anyone actually connected right now",
    /// independent of codepilot-api ever being up to ask. See
    /// idle_watchdog.rs: when this hits zero and stays there past a
    /// timeout, this machine suspends *itself*.
    pub live_connections: Arc<AtomicI64>,
}

/// RAII guard: increments `live_connections` on creation, decrements on
/// drop — so every WS handler just does `let _g = ConnGuard::new(&state);`
/// once at the top and every exit path (normal return, early return on a
/// handshake failure, panic-unwind) is covered automatically, with no risk
/// of a handler forgetting to decrement on some early-exit branch.
pub struct ConnGuard(Arc<AtomicI64>);

impl ConnGuard {
    pub fn new(state: &AppState) -> Self {
        state.live_connections.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Self(state.live_connections.clone())
    }
}

impl Drop for ConnGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
    }
}