mod b2_client;
mod credentials;
mod manifest;
mod snapshot;
mod walker;
mod watcher;
mod restore;

use manifest::Manifest;
use snapshot::SnapshotRoot;
use std::path::PathBuf;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let workspace_id = std::env::var("WORKSPACE_ID")?;
    let machine_secret = std::env::var("MACHINE_SECRET")?;
    let credentials_endpoint = std::env::var("CREDENTIALS_ENDPOINT")?;
    let debounce_secs: u64 = std::env::var("DEBOUNCE_SECS")
        .ok().and_then(|s| s.parse().ok()).unwrap_or(10);
    let max_concurrency: usize = std::env::var("MAX_CONCURRENCY")
        .ok().and_then(|s| s.parse().ok()).unwrap_or(16);

    let roots = vec![
        SnapshotRoot { name: "workspace".into(), local_path: PathBuf::from("/workspace") },
        SnapshotRoot { name: "sessions".into(), local_path: PathBuf::from("/root/.codepilot/sessions") },
        SnapshotRoot { name: "agent_yaml".into(), local_path: PathBuf::from("/opt/codepilot/agent.yaml") },
    ];

    let http = reqwest::Client::new();

    let scoped_key = credentials::fetch_scoped_key(&http, &credentials_endpoint, &machine_secret).await?;
    let mut session = b2_client::authorize(&http, &scoped_key).await?;

    // Check for --restore flag
    let is_restore = std::env::args().any(|a| a == "--restore");
    if is_restore {
        tracing::info!("Running in restore mode...");
        restore::run_restore(&http, &session, &workspace_id, &roots, max_concurrency).await?;
        return Ok(());
    }

    // TODO: on cold start, download any existing manifest.json for this
    // workspace_id so the first snapshot doesn't re-upload everything
    // that `restore` just pulled down unchanged.
    let mut previous_manifest: Option<Manifest> = None;
    let manifest_key = format!("workspaces/{workspace_id}/manifest.json");
    if let Ok(manifest_bytes) = b2_client::download_file(&http, &session, &manifest_key).await {
        if let Ok(m) = serde_json::from_slice(&manifest_bytes) {
            previous_manifest = Some(m);
            tracing::info!("Loaded previous manifest for delta-sync.");
        }
    }

    let mut change_rx = watcher::spawn_watcher(
        roots.iter().map(|r| r.local_path.clone()).collect(),
        debounce_secs,
    );
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;

    loop {
        tokio::select! {
            _ = change_rx.recv() => {
                tracing::info!("change detected");
            }
            _ = sigterm.recv() => {
                tracing::info!("SIGTERM received, running final snapshot");
                refresh_if_expired(&http, &credentials_endpoint, &machine_secret, &mut session).await?;
                snapshot::run_snapshot(&http, &session, &workspace_id, &roots, previous_manifest.as_ref(), max_concurrency).await?;
                break;
            }
        }

        refresh_if_expired(&http, &credentials_endpoint, &machine_secret, &mut session).await?;

        match snapshot::run_snapshot(&http, &session, &workspace_id, &roots, previous_manifest.as_ref(), max_concurrency).await {
            Ok(m) => previous_manifest = Some(m),
            Err(e) => tracing::error!("snapshot failed: {e:#}"),
        }
    }

    Ok(())
}

async fn refresh_if_expired(
    http: &reqwest::Client,
    endpoint: &str,
    machine_secret: &str,
    session: &mut b2_client::B2Session,
) -> anyhow::Result<()> {
    if chrono::Utc::now().timestamp() < session.expires_at {
        return Ok(());
    }
    tracing::info!("B2 credentials expired, re-fetching");
    let key = credentials::fetch_scoped_key(http, endpoint, machine_secret).await?;
    *session = b2_client::authorize(http, &key).await?;
    Ok(())
}