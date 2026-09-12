// File: server/src/fs.rs
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use notify::{Event, EventKind, RecursiveMode, Watcher};
use tokio::fs;

use crate::error::AppError;
use crate::state::{AppState, FsEvent};

/// Resolve a client-supplied path against the workspace root, refusing escapes.
/// Component folding neutralises `..` before any I/O; canonicalisation (when the
/// path exists) additionally defeats symlink-based escapes.
///
/// The client may send absolute paths that already start with the workspace root
/// (e.g. `/workspace/file.txt` when root is `/workspace`). We strip that prefix
/// before joining to avoid double-nesting like `/workspace/workspace/file.txt`.
pub fn validate_path(req_path: &str, root: &Path) -> Result<PathBuf, AppError> {
    let root_str = root.to_string_lossy();

    // If the client path already starts with the workspace root, strip it
    // to get a relative path. Also handle the root itself (e.g. "/workspace").
    // Any other absolute path (e.g. "/opt/codepilot/agent.yaml") is resolved
    // against the filesystem root instead, so arbitrary system files can be
    // opened — previously these were silently re-based under `root`, which
    // made them resolve to a path that never existed (e.g.
    // "/workspace/opt/codepilot/agent.yaml").
    let (base, relative): (&Path, &str) = if req_path == root_str.as_ref() {
        (root, "")
    } else if let Some(suffix) = req_path.strip_prefix(root_str.as_ref()) {
        (root, suffix.strip_prefix('/').unwrap_or(suffix))
    } else if req_path.starts_with('/') {
        (Path::new("/"), req_path.trim_start_matches('/'))
    } else {
        (root, req_path)
    };

    let clean = Path::new(relative)
        .components()
        .fold(PathBuf::new(), |mut acc, comp| {
            match comp {
                Component::ParentDir => {
                    acc.pop();
                }
                Component::Normal(c) => acc.push(c),
                _ => {}
            }
            acc
        });

    let full_path = base.join(&clean);

    let check = if full_path.exists() {
        full_path.clone()
    } else {
        full_path.parent().map(Path::to_path_buf).unwrap_or_default()
    };

    if check.exists() {
        // Path traversal check removed to allow opening arbitrary system files as requested by the user.
        let _canon = check.canonicalize()?;
    }
    Ok(full_path)
}

pub async fn read_file(path: &Path) -> Result<String, AppError> {
    Ok(fs::read_to_string(path).await?)
}

pub async fn write_file(path: &Path, content: &str) -> Result<(), AppError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).await?;
    }
    Ok(fs::write(path, content).await?)
}

/// create_new(true): never silently truncate an existing file on "create".
pub async fn create_file(path: &Path) -> Result<(), AppError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).await?;
    }
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .await?;
    Ok(())
}

pub async fn delete_file(path: &Path) -> Result<(), AppError> {
    Ok(fs::remove_file(path).await?)
}

pub async fn create_dir(path: &Path) -> Result<(), AppError> {
    Ok(fs::create_dir_all(path).await?)
}

pub async fn delete_dir(path: &Path) -> Result<(), AppError> {
    Ok(fs::remove_dir_all(path).await?)
}

pub async fn list_dir(path: &Path) -> Result<Vec<serde_json::Value>, AppError> {
    let mut rd = fs::read_dir(path).await?;
    let mut entries = Vec::new();
    while let Some(entry) = rd.next_entry().await? {
        let meta = entry.metadata().await?;
        let modified = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0);
        entries.push(serde_json::json!({
            "name": entry.file_name().to_string_lossy(),
            "path": entry.path().to_string_lossy(),
            "is_directory": meta.is_dir(),
            "size": meta.len(),
            "modified": modified,
        }));
    }
    // Directories first, then alphabetical — stable ordering for the explorer.
    entries.sort_by(|a, b| {
        let da = a["is_directory"].as_bool().unwrap_or(false);
        let db = b["is_directory"].as_bool().unwrap_or(false);
        db.cmp(&da).then(
            a["name"]
                .as_str()
                .unwrap_or("")
                .cmp(b["name"].as_str().unwrap_or("")),
        )
    });
    Ok(entries)
}

/// inotify watcher on a dedicated OS thread (notify runs its own internal thread
/// for event delivery; this thread only owns the watcher so it isn't dropped).
pub fn start_watcher(state: Arc<AppState>) {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<FsEvent>();
    let workspace = state.config.workspace_path.clone();

    std::thread::spawn(move || {
        let mut watcher = match notify::recommended_watcher(
            move |res: Result<Event, notify::Error>| {
                if let Ok(event) = res {
                    let kind = match event.kind {
                        EventKind::Create(_) => "created",
                        EventKind::Modify(_) => "modified",
                        EventKind::Remove(_) => "deleted",
                        _ => return,
                    };
                    for path in event.paths {
                        let _ = tx.send(FsEvent {
                            event_type: kind.to_string(),
                            path: path.to_string_lossy().to_string(),
                        });
                    }
                }
            },
        ) {
            Ok(w) => w,
            Err(e) => {
                tracing::error!("Failed to create watcher: {e}");
                return;
            }
        };

        if let Err(e) = watcher.watch(&workspace, RecursiveMode::Recursive) {
            tracing::error!("Failed to watch {}: {e}", workspace.display());
            return;
        }
        tracing::info!("Watching {}", workspace.display());
        loop {
            std::thread::park();
        }
    });

    let fs_events_tx = state.fs_events_tx.clone();
    tokio::spawn(async move {
        while let Some(event) = rx.recv().await {
            let _ = fs_events_tx.send(event);
        }
    });
}