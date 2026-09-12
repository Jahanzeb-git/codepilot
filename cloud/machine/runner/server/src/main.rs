// File: server/src/main.rs
use std::sync::Arc;

use tokio::sync::{broadcast, mpsc};
use tracing_subscriber::EnvFilter;

mod agent;
mod config;
mod error;
mod fs;
mod idle_watchdog;
mod routes;
mod state;
mod terminal;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    tracing::info!("codepilot-server booting");

    let cfg = Arc::new(config::Config::from_env());
    let (fs_tx, _fs_rx) = broadcast::channel::<state::FsEvent>(256);
    let (agent_inbound_tx, agent_inbound_rx) = mpsc::unbounded_channel::<Vec<u8>>();
    let (control_plane_tx, control_plane_rx) = mpsc::unbounded_channel::<Vec<u8>>();

    let state = Arc::new(state::AppState {
        config: cfg.clone(),
        fs_events_tx: fs_tx,
        control_clients: Arc::new(dashmap::DashMap::new()),
        agent_clients: Arc::new(dashmap::DashMap::new()),
        agent_inbound_tx,
        control_plane_tx,
        known_terminals: Arc::new(dashmap::DashMap::new()),
        live_connections: Arc::new(std::sync::atomic::AtomicI64::new(0)),
    });

    // Background planes
    fs::start_watcher(state.clone());
    terminal::start_control_plane_listener(state.clone(), control_plane_rx);
    tokio::spawn(agent::run_agent_bridge(state.clone(), agent_inbound_rx));
    idle_watchdog::start(state.clone());

    let app = routes::router(state).layer(tower_http::trace::TraceLayer::new_for_http());

    // Bind on the IPv6 unspecified address, not IPv4-only 0.0.0.0. Fly's
    // private 6PN network (used by codepilot-api's proxy to reach this
    // machine at <machine_id>.vm.<app>.internal) is IPv6-only — an
    // IPv4-only listener accepts local/IPv4 connections fine but has no
    // IPv6 socket at all, so any 6PN connection attempt gets an instant
    // ECONNREFUSED. On Linux, binding `[::]` with default socket options
    // is dual-stack, so this also keeps IPv4 working.
    let addr = std::net::SocketAddr::from(([0, 0, 0, 0, 0, 0, 0, 0], cfg.port));
    tracing::info!("listening on {addr}");
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind port");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("server error");
}

/// Fly.io stops machines with SIGTERM; also honour Ctrl+C for local dev.
async fn shutdown_signal() {
    let mut sigterm =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {},
        _ = sigterm.recv() => {},
    }
    tracing::info!("shutdown signal received; draining connections");
}