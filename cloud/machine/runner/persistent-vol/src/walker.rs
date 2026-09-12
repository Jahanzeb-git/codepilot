use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

/// Iterative (not recursive) so a deeply nested workspace can't blow the
/// call stack — same idea you used for `walk`, just stack-based instead
/// of function-recursive.
pub fn walk_sync(root: &Path) -> Vec<PathBuf> {
    if root.is_file() {
        return vec![root.to_path_buf()];
    }
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            if let Ok(ft) = entry.file_type() {
                if ft.is_dir() {
                    stack.push(path);
                } else if ft.is_file() {
                    out.push(path);
                }
            }
        }
    }
    out
}

pub async fn hash_file(path: &Path) -> anyhow::Result<(String, u64)> {
    // NOTE: reads the whole file into memory. Fine for source-file-sized
    // workspaces; if large binary assets show up later, switch to a
    // streaming hash (read in chunks) instead.
    let bytes = tokio::fs::read(path).await?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok((hex::encode(hasher.finalize()), bytes.len() as u64))
}