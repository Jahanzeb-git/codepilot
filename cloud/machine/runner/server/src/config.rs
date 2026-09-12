// File: server/src/config.rs
use std::env;
use std::path::PathBuf;

pub struct Config {
    pub workspace_path: PathBuf,
    pub static_path: PathBuf,
    pub port: u16,
    pub agent_runtime_sock: PathBuf,
    pub codepilot_control_sock: PathBuf,
    /// This machine's own secret, injected by codepilot-api at launch
    /// (`MACHINE_SECRET` env var) — used to authenticate the self-suspend
    /// call in idle_watchdog.rs. Empty string if unset (self-suspend is
    /// then a no-op, logged loudly, rather than a panic — a missing secret
    /// shouldn't take down the whole workspace).
    pub machine_secret: String,
    /// Base URL of codepilot-api (`CONTROL_PLANE_URL` env var), e.g.
    /// "https://codepilot-api.fly.dev". Used for the same self-suspend call.
    pub control_plane_url: String,
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            workspace_path: PathBuf::from(env::var("WORKSPACE_PATH").unwrap_or_else(|_| "/workspace".into())),
            static_path: PathBuf::from(env::var("STATIC_PATH").unwrap_or_else(|_| "./frontend/dist".into())),
            port: env::var("PORT").unwrap_or_else(|_| "8080".into()).parse().unwrap_or(8080),
            agent_runtime_sock: PathBuf::from(env::var("AGENT_RUNTIME_SOCK").unwrap_or_else(|_| "/tmp/agent_runtime.sock".into())),
            codepilot_control_sock: PathBuf::from(env::var("CODEPILOT_CONTROL_SOCK").unwrap_or_else(|_| "/tmp/codepilot_control.sock".into())),
            machine_secret: env::var("MACHINE_SECRET").unwrap_or_default(),
            control_plane_url: env::var("CONTROL_PLANE_URL").unwrap_or_else(|_| "https://codepilot-api.fly.dev".into()),
        }
    }
}