use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone)]
pub struct FileEntry {
    pub root: String,
    pub rel_path: String,
    pub size: u64,
    pub sha256: String,
    pub b2_key: String,
}

#[derive(Serialize, Deserialize, Clone)]
pub struct Manifest {
    pub workspace_id: String,
    pub created_at: String,
    pub roots: Vec<String>,
    pub files: Vec<FileEntry>,
}