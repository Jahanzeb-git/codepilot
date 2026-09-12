// File: server/src/idle_watchdog.rs
//
// This machine watches its OWN live browser-facing WebSocket count
// (state.live_connections — see state.rs's ConnGuard, wired into every
// /ws/control, /ws/agent, and /ws/terminal/* handler) and, if that count
// stays at zero continuously for IDLE_TIMEOUT, suspends itself by calling
// codepilot-api's `/machines/suspend` endpoint with its own machine secret.
//
// Why this lives here instead of relying solely on codepilot-api polling
// every machine: a browser tab closing kills every one of this machine's
// WS connections almost immediately (standard browser behavior), so this
// machine knows "nobody is here" faster and more directly than any
// external poller could — and it doesn't require codepilot-api to stay
// running 24/7 just to notice. The outbound HTTPS call below is enough by
// itself to wake codepilot-api on demand (Fly's auto_start_machines) for
// the one request it needs to handle, then it can go back to sleep.
//
// A page *refresh* also briefly drops every connection to zero, which is
// exactly why this is timeout-based rather than instant: the debounce
// window gives a reconnecting browser time to re-establish before this
// machine concludes the user is actually gone.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use crate::state::AppState;

/// How long live_connections must stay continuously at zero before we
/// conclude the tab is actually closed (not just reloading/reconnecting).
const IDLE_TIMEOUT: Duration = Duration::from_secs(90);
/// How often we sample the connection count.
const POLL_INTERVAL: Duration = Duration::from_secs(5);

pub fn start(state: Arc<AppState>) {
    tokio::spawn(async move {
        let mut idle_for = Duration::ZERO;
        let mut suspend_requested = false;

        loop {
            tokio::time::sleep(POLL_INTERVAL).await;

            let connected = state.live_connections.load(Ordering::Relaxed) > 0;
            if connected {
                if idle_for > Duration::ZERO {
                    tracing::info!("idle_watchdog: connection re-established, resetting idle timer");
                }
                idle_for = Duration::ZERO;
                suspend_requested = false;
                continue;
            }

            idle_for += POLL_INTERVAL;

            if idle_for >= IDLE_TIMEOUT && !suspend_requested {
                tracing::warn!(
                    "idle_watchdog: no live WS connections for {:?} (>= {:?}); requesting self-suspend",
                    idle_for, IDLE_TIMEOUT,
                );
                // Only try once per idle episode — if it fails, we'll be
                // suspended by Fly's process exit soon anyway once
                // codepilot-api's own periodic sweep catches it, and
                // hammering the endpoint on every 5s tick if the request
                // is failing (network blip, codepilot-api mid-deploy)
                // wouldn't make it more likely to succeed.
                suspend_requested = true;
                request_self_suspend(&state).await;
            }
        }
    });
}

async fn request_self_suspend(state: &Arc<AppState>) {
    let secret = state.config.machine_secret.clone();
    if secret.is_empty() {
        tracing::error!("idle_watchdog: MACHINE_SECRET is not set — cannot self-suspend");
        return;
    }
    let url = format!("{}/machines/suspend", state.config.control_plane_url.trim_end_matches('/'));

    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            tracing::error!("idle_watchdog: failed to build HTTP client: {e}");
            return;
        }
    };

    match client
        .post(&url)
        .header("X-Machine-Secret", secret)
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => {
            tracing::info!("idle_watchdog: self-suspend request accepted ({url})");
        }
        Ok(resp) => {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            tracing::error!("idle_watchdog: self-suspend request rejected: {status} {body}");
        }
        Err(e) => {
            tracing::error!("idle_watchdog: self-suspend request failed: {e}");
        }
    }
}
