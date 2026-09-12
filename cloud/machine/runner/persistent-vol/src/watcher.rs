use notify_debouncer_full::{new_debouncer, DebounceEventResult, notify::Watcher};
use std::path::PathBuf;
use std::time::Duration;
use tokio::sync::mpsc;

/// Runs inotify on a dedicated OS thread (notify's API is blocking).
/// The channel has capacity 1: if a snapshot is already pending, extra
/// change events are dropped rather than queued — we only need "something
/// changed," not a count of how many times.
pub fn spawn_watcher(paths: Vec<PathBuf>, debounce_secs: u64) -> mpsc::Receiver<()> {
    let (tx, rx) = mpsc::channel(1);

    std::thread::spawn(move || {
        let (raw_tx, raw_rx) = std::sync::mpsc::channel::<DebounceEventResult>();
        let mut debouncer = new_debouncer(Duration::from_secs(debounce_secs), None, raw_tx)
            .expect("failed to create fs watcher");

        for p in &paths {
            if let Err(e) = debouncer.watcher().watch(p, notify_debouncer_full::notify::RecursiveMode::Recursive) {
                tracing::warn!("failed to watch {p:?}: {e}");
            }
        }

        for result in raw_rx {
            if result.is_ok() {
                let _ = tx.blocking_send(());
            }
        }
    });

    rx
}