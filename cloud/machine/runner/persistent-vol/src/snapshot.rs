use crate::b2_client::{self, B2Session};
use crate::manifest::{FileEntry, Manifest};
use crate::walker::{hash_file, walk_sync};
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::Semaphore;

pub struct SnapshotRoot {
    pub name: String,
    pub local_path: PathBuf,
}

pub async fn run_snapshot(
    client: &reqwest::Client,
    session: &B2Session,
    workspace_id: &str,
    roots: &[SnapshotRoot],
    previous: Option<&Manifest>,
    max_concurrency: usize,
) -> anyhow::Result<Manifest> {
    // 1. Walk every root off the async runtime (blocking syscalls)
    let mut discovered: Vec<(String, PathBuf, PathBuf)> = Vec::new();
    for root in roots {
        let base = root.local_path.clone();
        let files = tokio::task::spawn_blocking({
            let base = base.clone();
            move || walk_sync(&base)
        })
        .await?;
        for f in files {
            discovered.push((root.name.clone(), base.clone(), f));
        }
    }

    // 2. Hash concurrently, bounded
    let sem = Arc::new(Semaphore::new(max_concurrency));
    let mut hash_tasks = Vec::new();
    for (root_name, base, path) in discovered {
        let sem = sem.clone();
        hash_tasks.push(tokio::spawn(async move {
            let _permit = sem.acquire_owned().await.unwrap();
            let (sha256, size) = hash_file(&path).await?;
            let rel = if path == base {
                path.file_name().unwrap().to_string_lossy().to_string()
            } else {
                path.strip_prefix(&base).unwrap().to_string_lossy().to_string()
            };
            anyhow::Ok((root_name, rel, path, sha256, size))
        }));
    }

    let mut current = Vec::new();
    for t in hash_tasks {
        current.push(t.await??);
    }

    // 3. Diff against previous manifest — unchanged (root, rel, sha256)
    //    triples get skipped, everything else re-uploads
    let unchanged: HashSet<(String, String, String)> = previous
        .map(|m| {
            m.files
                .iter()
                .map(|f| (f.root.clone(), f.rel_path.clone(), f.sha256.clone()))
                .collect()
        })
        .unwrap_or_default();

    let mut new_files = Vec::new();
    let mut to_upload = Vec::new();

    for (root_name, rel, path, sha256, size) in current {
        let b2_key = format!("workspaces/{workspace_id}/{root_name}/{rel}");
        let key_tuple = (root_name.clone(), rel.clone(), sha256.clone());
        if !unchanged.contains(&key_tuple) {
            to_upload.push((path, b2_key.clone()));
        }
        new_files.push(FileEntry { root: root_name, rel_path: rel, size, sha256, b2_key });
    }

    tracing::info!("{} files total, {} changed", new_files.len(), to_upload.len());

    // 4. Upload changed files concurrently, bounded — one upload URL per
    //    task (B2 recommends not sharing an upload URL across concurrent
    //    requests)
    let sem = Arc::new(Semaphore::new(max_concurrency));
    let mut upload_tasks = Vec::new();
    for (path, b2_key) in to_upload {
        let sem = sem.clone();
        let client = client.clone();
        let session = session.clone();
        upload_tasks.push(tokio::spawn(async move {
            let _permit = sem.acquire_owned().await.unwrap();
            let upload_url = b2_client::get_upload_url(&client, &session).await?;
            let bytes = tokio::fs::read(&path).await?;
            b2_client::upload_file(&client, &upload_url, &b2_key, bytes).await?;
            anyhow::Ok(())
        }));
    }
    for t in upload_tasks {
        t.await??;
    }

    // 5. Manifest uploads LAST — it's the commit marker. A restore that
    //    finds no manifest (or a stale one) knows the prior snapshot
    //    never fully completed.
    let manifest = Manifest {
        workspace_id: workspace_id.to_string(),
        created_at: chrono::Utc::now().to_rfc3339(),
        roots: roots.iter().map(|r| r.name.clone()).collect(),
        files: new_files,
    };
    let manifest_bytes = serde_json::to_vec_pretty(&manifest)?;
    let manifest_key = format!("workspaces/{workspace_id}/manifest.json");
    let upload_url = b2_client::get_upload_url(client, session).await?;
    b2_client::upload_file(client, &upload_url, &manifest_key, manifest_bytes).await?;

    Ok(manifest)
}