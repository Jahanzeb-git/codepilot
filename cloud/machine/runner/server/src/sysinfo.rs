// File: server/src/sysinfo.rs
//
// Backing data for the profile modal. Every probe here is read-only and
// best-effort: a missing file, a pip that isn't on PATH, or an unreachable
// api.github.com degrades to `None` rather than failing the request — the
// modal is informational and must still render inside a sandbox that has no
// outbound egress at all.
//
// Three subtleties worth stating explicitly:
//
//   * A workspace runs in its own cgroup, so /proc/meminfo reports the
//     HOST's RAM and is actively misleading. The cgroup files are the only
//     honest answer for the sandbox's own memory budget — and the cgroup2
//     mount root has no memory.max of its own, so the cgroup path has to be
//     resolved out of /proc/self/cgroup rather than hardcoded.
//   * The environment block is an ALLOWLIST, never a filter. This process
//     holds live provider credentials (LLM_API_KEY, DEEPSEEK_API_KEY, …); a
//     denylist would leak the first key nobody thought to add.
//   * The release check is cached in-process. GitHub's unauthenticated API
//     allows 60 requests/hour per IP and every open of the modal probes it.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

const RELEASE_API: &str = "https://api.github.com/repos/Jahanzeb-git/codepilot/releases/latest";
const REPOSITORY_URL: &str = "https://github.com/Jahanzeb-git/codepilot/";
const RUNTIME_PACKAGE: &str = "codepilot-ai";
const CGROUP_ROOT: &str = "/sys/fs/cgroup";

const RELEASE_TTL: Duration = Duration::from_secs(600);
/// A failed probe retries sooner, so one network blip doesn't pin
/// "unreachable" on screen for ten minutes.
const RELEASE_FAILURE_TTL: Duration = Duration::from_secs(60);
/// Kept comfortably under the control socket's 15s request timeout.
const RELEASE_TIMEOUT: Duration = Duration::from_secs(6);

/// Rendered in the modal, in this order. Anything absent from this list is
/// never read out of the process environment.
const SAFE_ENV: &[&str] = &[
    "SUDO_USER",
    "USER",
    "LOGNAME",
    "HOME",
    "SHELL",
    "PWD",
    "HOSTNAME",
    "LANG",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "XDG_CURRENT_DESKTOP",
    "CODEPILOT_LOCAL",
    "WORKSPACE_PATH",
    "STATIC_PATH",
    "PATH",
];

// ------------------------------------------------------------------ payload

#[derive(Serialize)]
pub struct SystemInfo {
    pub server: ServerInfo,
    pub runtime: RuntimeInfo,
    pub sandbox: SandboxInfo,
    pub environment: Vec<EnvVar>,
    pub repository: &'static str,
}

#[derive(Serialize)]
pub struct ServerInfo {
    /// Version of the binary answering this request.
    pub version: String,
    /// "build" when the image builder injected CODEPILOT_VERSION, "crate"
    /// when we fell back to Cargo.toml (i.e. a dev `cargo run`).
    pub source: &'static str,
    pub commit: Option<String>,
    /// e.g. "linux-amd64" — names the release artifact built for this machine.
    pub target: &'static str,
    pub latest: Option<ReleaseInfo>,
    pub latest_error: Option<String>,
    /// None whenever either version isn't a plain numeric version, which is
    /// the honest answer for a dev build ("dev", "main", a sha).
    pub update_available: Option<bool>,
}

#[derive(Serialize, Clone)]
pub struct ReleaseInfo {
    pub tag: String,
    pub name: Option<String>,
    pub url: String,
    pub published_at: Option<String>,
    pub assets: Vec<ReleaseAsset>,
    /// The asset this machine would actually download, if the release has one.
    pub matching_asset: Option<ReleaseAsset>,
}

#[derive(Serialize, Clone)]
pub struct ReleaseAsset {
    pub name: String,
    pub size: u64,
}

#[derive(Serialize)]
pub struct RuntimeInfo {
    pub package: &'static str,
    pub version: Option<String>,
    pub location: Option<String>,
    pub python: Option<String>,
}

#[derive(Serialize)]
pub struct SandboxInfo {
    pub cpu_model: Option<String>,
    /// Usable cores in this sandbox's cpuset — not the host's core count.
    pub cpu_cores: Option<usize>,
    /// Fractional cores from cgroup cpu.max, when the sandbox is throttled.
    pub cpu_quota_cores: Option<f64>,
    pub cpuset: Option<String>,
    pub memory_limit_bytes: Option<u64>,
    pub memory_used_bytes: Option<u64>,
    pub memory_free_bytes: Option<u64>,
    pub memory_peak_bytes: Option<u64>,
    /// "cgroup" (sandbox limits) | "proc" (host fallback) | "none".
    pub memory_source: &'static str,
    pub host_memory_total_bytes: Option<u64>,
    pub workspace_path: String,
    pub containerized: bool,
}

#[derive(Serialize)]
pub struct EnvVar {
    pub key: String,
    pub value: String,
}

// ------------------------------------------------------------------ collect

pub async fn collect(workspace_path: &Path) -> SystemInfo {
    let (version, source) = server_version();
    let target = target_triple();
    let (latest, latest_error) = probe_latest_release(target).await;

    let update_available = match &latest {
        Some(release) => compare_versions(&release.tag, &version),
        None => None,
    };

    SystemInfo {
        server: ServerInfo {
            version,
            source,
            commit: option_env!("CODEPILOT_COMMIT").map(|c| c.trim().to_string()),
            target,
            latest,
            latest_error,
            update_available,
        },
        runtime: runtime_info(),
        sandbox: sandbox_info(workspace_path),
        environment: safe_environment(),
        repository: REPOSITORY_URL,
    }
}

fn server_version() -> (String, &'static str) {
    match option_env!("CODEPILOT_VERSION") {
        Some(v) if !v.trim().is_empty() => (v.trim().to_string(), "build"),
        _ => (env!("CARGO_PKG_VERSION").to_string(), "crate"),
    }
}

/// Names the release artifact this sandbox would run, so "which binary is
/// running currently" can be answered against what upstream actually ships.
fn target_triple() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "linux-amd64",
        "aarch64" => "linux-arm64",
        _ => "",
    }
}

// ------------------------------------------------------------------ versions

/// `v1.1.11` -> [1, 1, 11]; trailing `-rc1`/`+build` are tolerated by
/// stopping at the first non-digit of each segment. None for anything that
/// isn't version-shaped ("main", a sha, an empty string).
fn parse_version(raw: &str) -> Option<Vec<u64>> {
    let trimmed = raw.trim().trim_start_matches('v');
    if trimmed.is_empty() {
        return None;
    }
    let mut parts = Vec::new();
    for segment in trimmed.split('.') {
        let digits: String = segment.chars().take_while(|c| c.is_ascii_digit()).collect();
        if digits.is_empty() {
            return None;
        }
        parts.push(digits.parse::<u64>().ok()?);
    }
    Some(parts)
}

/// None when either side isn't comparable, so the UI can say "development
/// build" instead of guessing.
fn compare_versions(latest: &str, running: &str) -> Option<bool> {
    match (parse_version(latest), parse_version(running)) {
        (Some(mut l), Some(mut r)) => {
            let width = l.len().max(r.len());
            l.resize(width, 0);
            r.resize(width, 0);
            Some(l > r)
        }
        _ => None,
    }
}

// ------------------------------------------------------------------ release

#[derive(Deserialize)]
struct GhRelease {
    tag_name: String,
    name: Option<String>,
    html_url: Option<String>,
    published_at: Option<String>,
    #[serde(default)]
    assets: Vec<GhAsset>,
}

#[derive(Deserialize)]
struct GhAsset {
    name: String,
    #[serde(default)]
    size: u64,
}

#[derive(Clone)]
struct CachedRelease {
    at: Instant,
    release: Option<ReleaseInfo>,
    error: Option<String>,
}

static RELEASE_CACHE: Mutex<Option<CachedRelease>> = Mutex::new(None);

async fn probe_latest_release(target: &str) -> (Option<ReleaseInfo>, Option<String>) {
    // The cache guard is confined to this expression so it is never live
    // across an .await — the whole handler future has to stay Send.
    let cached = match RELEASE_CACHE.lock() {
        Ok(cache) => cache.as_ref().and_then(|c| {
            let ttl = if c.release.is_some() { RELEASE_TTL } else { RELEASE_FAILURE_TTL };
            if c.at.elapsed() < ttl {
                Some((c.release.clone(), c.error.clone()))
            } else {
                None
            }
        }),
        Err(_) => None,
    };
    if let Some(hit) = cached {
        return hit;
    }

    let fresh = fetch_latest_release(target).await;
    if let Err(err) = fresh.as_ref() {
        tracing::warn!("sysinfo: latest release probe failed: {err}");
    }
    let cached = CachedRelease {
        at: Instant::now(),
        release: fresh.as_ref().ok().cloned(),
        error: fresh.as_ref().err().cloned(),
    };
    if let Ok(mut cache) = RELEASE_CACHE.lock() {
        *cache = Some(cached);
    }
    match fresh {
        Ok(release) => (Some(release), None),
        Err(err) => (None, Some(err)),
    }
}

async fn fetch_latest_release(target: &str) -> Result<ReleaseInfo, String> {
    let client = reqwest::Client::builder()
        .timeout(RELEASE_TIMEOUT)
        .user_agent(format!("codepilot-workspace/{}", server_version().0))
        .build()
        .map_err(|e| format!("http client: {e}"))?;

    let resp = client
        .get(RELEASE_API)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("github.com unreachable: {e}"))?;

    let status = resp.status();
    if !status.is_success() {
        return Err(format!("github api returned {status}"));
    }

    let release: GhRelease = resp
        .json()
        .await
        .map_err(|e| format!("unexpected github payload: {e}"))?;

    let assets: Vec<ReleaseAsset> = release
        .assets
        .into_iter()
        .map(|a| ReleaseAsset { name: a.name, size: a.size })
        .collect();

    let matching_asset = if target.is_empty() {
        None
    } else {
        assets.iter().find(|a| a.name.ends_with(target)).cloned()
    };

    let url = release
        .html_url
        .clone()
        .unwrap_or_else(|| format!("{REPOSITORY_URL}releases/tag/{}", release.tag_name));

    Ok(ReleaseInfo {
        url,
        tag: release.tag_name,
        name: release.name,
        published_at: release.published_at,
        assets,
        matching_asset,
    })
}

// ------------------------------------------------------------------ runtime

/// Tried in order: the image ships pip3, but a hand-rolled environment may
/// only have the module form.
const PIP_COMMANDS: &[&[&str]] = &[&["pip3"], &["pip"], &["python3", "-m", "pip"]];

fn runtime_info() -> RuntimeInfo {
    let pip = pip_show(RUNTIME_PACKAGE);
    RuntimeInfo {
        package: RUNTIME_PACKAGE,
        version: pip.as_ref().and_then(|p| p.version.clone()),
        location: pip.as_ref().and_then(|p| p.location.clone()),
        python: python_version(),
    }
}

struct PipInfo {
    version: Option<String>,
    location: Option<String>,
}

fn pip_show(package: &str) -> Option<PipInfo> {
    for candidate in PIP_COMMANDS {
        let program = match candidate.first() {
            Some(p) => p,
            None => continue,
        };
        let output = match Command::new(program)
            .args(&candidate[1..])
            .args(["show", package])
            .output()
        {
            Ok(o) => o,
            Err(_) => continue,
        };
        if !output.status.success() {
            continue;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        if let Some(info) = parse_pip_show(&text) {
            return Some(info);
        }
    }
    None
}

fn parse_pip_show(output: &str) -> Option<PipInfo> {
    let mut version = None;
    let mut location = None;

    for line in output.lines() {
        if let Some(rest) = line.strip_prefix("Version:") {
            version = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("Location:") {
            location = Some(rest.trim().to_string());
        }
    }

    if version.is_none() && location.is_none() {
        None
    } else {
        Some(PipInfo { version, location })
    }
}

fn python_version() -> Option<String> {
    for program in ["python3", "python"] {
        let output = match Command::new(program).arg("--version").output() {
            Ok(o) => o,
            Err(_) => continue,
        };
        if !output.status.success() {
            continue;
        }
        // --version writes to stdout since 3.4; older builds use stderr.
        let text = if output.stdout.is_empty() {
            String::from_utf8_lossy(&output.stderr).to_string()
        } else {
            String::from_utf8_lossy(&output.stdout).to_string()
        };
        let trimmed = text.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.trim_start_matches("Python ").to_string());
        }
    }
    None
}

// ------------------------------------------------------------------ sandbox

fn sandbox_info(workspace_path: &Path) -> SandboxInfo {
    let cgroup = cgroup_dir();
    let (cpu_model, cpuinfo_cores) = cpuinfo();

    let cpuset = cgroup
        .as_ref()
        .and_then(|dir| read_trimmed(&dir.join("cpuset.cpus.effective")));

    let cpu_cores = cpuset
        .as_deref()
        .and_then(count_cpus)
        .or(cpuinfo_cores)
        .or_else(|| std::thread::available_parallelism().ok().map(|n| n.get()));

    let cpu_quota_cores = match cgroup.as_ref().and_then(|dir| read_trimmed(&dir.join("cpu.max"))) {
        Some(raw) => parse_cpu_max(&raw),
        None => None,
    };

    let host = meminfo();
    let mut memory_source = "none";
    let mut used = None;
    let mut peak = None;
    let mut limit = None;

    if let Some(dir) = cgroup.as_ref() {
        used = read_u64(&dir.join("memory.current"));
        peak = read_u64(&dir.join("memory.peak"))
            .or_else(|| read_u64(&dir.join("memory.max_usage_in_bytes")));
        limit = read_u64(&dir.join("memory.max"))
            .or_else(|| read_u64(&dir.join("memory.limit_in_bytes")));
        if used.is_some() {
            memory_source = "cgroup";
        }
    }

    if memory_source == "none" {
        if let Some((total, available)) = host {
            limit = Some(total);
            used = Some(total.saturating_sub(available));
            memory_source = "proc";
        }
    }

    if limit.is_none() {
        limit = host.map(|(total, _)| total);
    }

    let free = match memory_source {
        "proc" => host.map(|(_, available)| available),
        _ => match (limit, used) {
            (Some(l), Some(u)) => Some(l.saturating_sub(u)),
            _ => None,
        },
    };

    SandboxInfo {
        cpu_model,
        cpu_cores,
        cpu_quota_cores,
        cpuset,
        memory_limit_bytes: limit,
        memory_used_bytes: used,
        memory_free_bytes: free,
        memory_peak_bytes: peak,
        memory_source,
        host_memory_total_bytes: host.map(|(total, _)| total),
        workspace_path: workspace_path.to_string_lossy().to_string(),
        containerized: cgroup.is_some(),
    }
}

/// Resolves this process's own cgroup directory. The cgroup2 mount root is
/// shared by every cgroup on the host, so `memory.max` lives in the
/// subdirectory named by /proc/self/cgroup's unified line (`0::/codepilot-workspace`).
fn cgroup_dir() -> Option<PathBuf> {
    let content = match std::fs::read_to_string("/proc/self/cgroup") {
        Ok(c) => c,
        Err(_) => return None,
    };

    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(3, ':').collect();
        if parts.len() != 3 {
            continue;
        }
        // Only the unified (cgroup v2) line has an empty controller list;
        // every v1 line carries a comma-separated controller set.
        if !parts[1].is_empty() {
            continue;
        }
        let relative = parts[2].trim().trim_start_matches('/');
        let dir = if relative.is_empty() {
            PathBuf::from(CGROUP_ROOT)
        } else {
            Path::new(CGROUP_ROOT).join(relative)
        };
        if dir.is_dir() {
            return Some(dir);
        }
    }

    // Fall back to the root only if it actually exposes cgroup data.
    let root = PathBuf::from(CGROUP_ROOT);
    if root.join("memory.max").exists() || root.join("memory.current").exists() {
        Some(root)
    } else {
        None
    }
}

/// "0-11,14" -> 13. None if the list is unparseable.
fn count_cpus(list: &str) -> Option<usize> {
    let mut total = 0usize;
    for part in list.split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        match part.split_once('-') {
            Some((low, high)) => {
                let low: usize = low.trim().parse().ok()?;
                let high: usize = high.trim().parse().ok()?;
                if high >= low {
                    total += high - low + 1;
                }
            }
            None => {
                part.parse::<usize>().ok()?;
                total += 1;
            }
        }
    }
    if total > 0 {
        Some(total)
    } else {
        None
    }
}

/// `cpu.max` is "<quota> <period>" in µs, with the literal "max" meaning
/// unthrottled.
fn parse_cpu_max(raw: &str) -> Option<f64> {
    let mut fields = raw.split_whitespace();
    let quota = fields.next()?;
    let period: f64 = fields.next()?.parse().ok()?;
    let quota: f64 = quota.parse().ok()?;
    if period <= 0.0 {
        return None;
    }
    Some(quota / period)
}

/// (MemTotal, MemAvailable) in bytes. Host-wide — see the file header.
fn meminfo() -> Option<(u64, u64)> {
    let content = match std::fs::read_to_string("/proc/meminfo") {
        Ok(c) => c,
        Err(_) => return None,
    };

    let mut total = None;
    let mut available = None;
    let mut free = None;

    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            total = parse_kib(rest);
        } else if let Some(rest) = line.strip_prefix("MemAvailable:") {
            available = parse_kib(rest);
        } else if let Some(rest) = line.strip_prefix("MemFree:") {
            free = parse_kib(rest);
        }
    }

    match (total, available.or(free)) {
        (Some(t), Some(a)) => Some((t, a)),
        _ => None,
    }
}

fn parse_kib(field: &str) -> Option<u64> {
    let digits: String = field.trim().chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return None;
    }
    digits.parse::<u64>().ok().map(|kib| kib * 1024)
}

fn cpuinfo() -> (Option<String>, Option<usize>) {
    let content = match std::fs::read_to_string("/proc/cpuinfo") {
        Ok(c) => c,
        Err(_) => return (None, None),
    };

    let mut model = None;
    let mut cores = 0usize;

    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("model name") {
            if model.is_none() {
                if let Some((_, value)) = rest.split_once(':') {
                    let value = value.trim();
                    if !value.is_empty() {
                        model = Some(value.to_string());
                    }
                }
            }
        } else if line.starts_with("processor") {
            cores += 1;
        }
    }

    (model, if cores > 0 { Some(cores) } else { None })
}

// ------------------------------------------------------------------ helpers

fn read_u64(path: &Path) -> Option<u64> {
    std::fs::read_to_string(path).ok()?.trim().parse().ok()
}

fn read_trimmed(path: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(path).ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn safe_environment() -> Vec<EnvVar> {
    SAFE_ENV
        .iter()
        .filter_map(|key| match std::env::var(key) {
            Ok(value) if !value.trim().is_empty() => Some(EnvVar {
                key: (*key).to_string(),
                value,
            }),
            _ => None,
        })
        .collect()
}
