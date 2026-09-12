use crate::b2_client::{self, B2Session};
use crate::manifest::Manifest;
use crate::snapshot::SnapshotRoot;
use crate::walker::hash_file;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::Semaphore;

pub async fn run_restore(
    client: &reqwest::Client,
    session: &B2Session,
    workspace_id: &str,
    roots: &[SnapshotRoot],
    max_concurrency: usize,
) -> anyhow::Result<Option<Manifest>> {
    let manifest_key = format!("workspaces/{workspace_id}/manifest.json");
    
    let manifest_bytes = match b2_client::download_file(client, session, &manifest_key).await {
        Ok(b) => b,
        Err(e) => {
            tracing::info!("No manifest found or download failed (brand new workspace?): {e}");
            return Ok(None);
        }
    };

    let manifest: Manifest = serde_json::from_slice(&manifest_bytes)?;
    tracing::info!("Found manifest from {}. Restoring {} files...", manifest.created_at, manifest.files.len());

    let root_map: HashMap<String, PathBuf> = roots
        .iter()
        .map(|r| (r.name.clone(), r.local_path.clone()))
        .collect();

    let sem = Arc::new(Semaphore::new(max_concurrency));
    let mut tasks = Vec::new();

    for entry in &manifest.files {
        let root_path = match root_map.get(&entry.root) {
            Some(p) => p,
            None => {
                tracing::warn!("Root {} not found in local config, skipping", entry.root);
                continue;
            }
        };

        let target_path = root_path.join(&entry.rel_path);
        let b2_key = entry.b2_key.clone();
        let expected_sha = entry.sha256.clone();
        
        let client = client.clone();
        let session = session.clone();
        let sem = sem.clone();

        tasks.push(tokio::spawn(async move {
            let _permit = sem.acquire_owned().await.unwrap();

            // If the file already exists, check the hash first.
            // If the hash matches what is in B2, the file is already up to date — skip it.
            // If the hash DOESN'T match (e.g. a stale image default replaced the user's
            // customised file after an image update), we MUST overwrite it with the B2 copy.
            if target_path.exists() {
                if let Ok((local_sha, _)) = hash_file(&target_path).await {
                    if local_sha == expected_sha {
                        return anyhow::Ok(());
                    }
                    // Hash mismatch — fall through to download and overwrite.
                    tracing::info!(
                        "Hash mismatch for {:?}: local != b2, overwriting with B2 copy.",
                        target_path
                    );
                }
            }

            if let Some(parent) = target_path.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }

            let bytes = b2_client::download_file(&client, &session, &b2_key).await?;
            // Use write() which creates OR truncates-and-overwrites. This is intentional:
            // the B2 manifest is the authoritative source of truth for persisted files.
            tokio::fs::write(&target_path, bytes).await?;
            anyhow::Ok(())
        }));
    }

    let mut success = 0;
    for t in tasks {
        match t.await? {
            Ok(_) => success += 1,
            Err(e) => tracing::error!("Failed to restore a file: {e}"),
        }
    }

    tracing::info!("Restore complete: {success}/{} files.", manifest.files.len());
    Ok(Some(manifest))
}
