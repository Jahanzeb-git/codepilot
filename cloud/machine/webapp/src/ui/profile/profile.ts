import { controlSocket } from "../../core/controlSocket";
import type { SystemInfo } from "../../core/types";
import { icon } from "../icons";

const REPOSITORY_URL = "https://github.com/Jahanzeb-git/codepilot/";
const REPOSITORY_LABEL = "github.com/Jahanzeb-git/codepilot";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const decimals = unit === 0 || value >= 100 ? 0 : 1;
  return `${value.toFixed(decimals)} ${units[unit]}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function percentOf(used: number | null, limit: number | null): number | null {
  if (used === null || limit === null || limit <= 0) return null;
  return Math.min(100, Math.max(0, (used / limit) * 100));
}

function row(key: string, value: string, mono = false): string {
  return `<div class="profile-row">
      <span class="profile-key">${key}</span>
      <span class="profile-value${mono ? " mono" : ""}">${value}</span>
    </div>`;
}

/**
 * The left-sidebar profile button's modal: which binary this sandbox is
 * running, which runtime it's running on, and what the sandbox itself was
 * given. Mirrors SettingsPanel's overlay/open/close contract so the two
 * modals behave identically.
 */
export class ProfilePanel {
  private overlay: HTMLElement;
  private body: HTMLElement;
  private nameEl: HTMLElement;
  private metaEl: HTMLElement;
  private repoEl: HTMLAnchorElement;
  private refreshBtn: HTMLButtonElement;

  private info: SystemInfo | null = null;
  private error = "";
  private loading = false;

  constructor(parent: HTMLElement) {
    this.overlay = document.createElement("div");
    this.overlay.className = "profile-overlay";
    this.overlay.hidden = true;
    this.overlay.innerHTML = `
      <div class="profile-modal" role="dialog" aria-modal="true" aria-label="Profile and workspace information">
        <div class="profile-header">
          <div class="profile-identity">
            <div class="profile-avatar">${icon("user")}</div>
            <div class="profile-identity-text">
              <div class="profile-name"></div>
              <a class="profile-repo" href="${REPOSITORY_URL}" target="_blank" rel="noopener noreferrer">${REPOSITORY_LABEL}</a>
            </div>
          </div>
          <div class="profile-actions">
            <button class="icon-btn" data-refresh title="Refresh">${icon("refresh")}</button>
            <button class="icon-btn" data-close title="Close">${icon("close")}</button>
          </div>
        </div>
        <div class="profile-body"></div>
      </div>
    `;
    parent.appendChild(this.overlay);

    this.body = this.overlay.querySelector<HTMLElement>(".profile-body")!;
    this.nameEl = this.overlay.querySelector<HTMLElement>(".profile-name")!;
    this.metaEl = document.createElement("div");
    this.metaEl.className = "profile-meta";
    this.nameEl.insertAdjacentElement("afterend", this.metaEl);
    this.repoEl = this.overlay.querySelector<HTMLAnchorElement>(".profile-repo")!;
    this.refreshBtn = this.overlay.querySelector<HTMLButtonElement>("[data-refresh]")!;

    this.overlay.querySelector("[data-close]")!.addEventListener("click", () => this.close());
    this.refreshBtn.addEventListener("click", () => this.load());
    this.overlay.addEventListener("click", (e) => {
      if (e.target === this.overlay) this.close();
    });

    this.render();
  }

  open() {
    this.overlay.hidden = false;
    this.load();
  }

  close() {
    this.overlay.hidden = true;
  }

  private async load() {
    if (this.loading) return;
    this.loading = true;
    this.error = "";
    this.overlay.classList.add("is-loading");
    this.refreshBtn.disabled = true;
    this.render();

    try {
      this.info = await controlSocket.systemInfo();
    } catch (err) {
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
      this.overlay.classList.remove("is-loading");
      this.refreshBtn.disabled = false;
      this.render();
    }
  }

  private env(key: string): string | null {
    const entry = this.info?.environment.find((item) => item.key === key);
    return entry ? entry.value : null;
  }

  private render() {
    const info = this.info;

    // Hero line: who this workspace was launched for. SUDO_USER is the
    // invoking account (everything in the sandbox runs as root); USER is the
    // account inside the sandbox.
    const launchUser = this.env("SUDO_USER");
    const sandboxUser = this.env("USER");
    this.nameEl.textContent = launchUser || sandboxUser || "codepilot workspace";
    this.metaEl.textContent = [
      launchUser && sandboxUser ? `runs as ${sandboxUser}` : null,
      info?.server.target || null,
      info?.sandbox.containerized ? "isolated sandbox" : null,
    ]
      .filter(Boolean)
      .join(" · ");
    this.repoEl.href = info?.repository || REPOSITORY_URL;

    if (!info) {
      this.body.innerHTML = this.loading
        ? `<div class="profile-empty">${icon("spinner", "spin")}<span>Reading workspace details…</span></div>`
        : `<div class="profile-empty profile-empty-error">
             ${icon("info")}
             <span>${escapeHtml(this.error || "No details available.")}</span>
             <button class="btn" data-retry>Try again</button>
           </div>`;
      this.body.querySelector("[data-retry]")?.addEventListener("click", () => this.load());
      return;
    }

    this.body.innerHTML = `
      <div class="profile-grid">
        ${this.renderVersion(info)}
        ${this.renderRuntime(info)}
        ${this.renderSandbox(info)}
        ${this.renderEnvironment(info)}
      </div>
      <div class="profile-footer">
        <a class="profile-footer-link" href="${info.repository}" target="_blank" rel="noopener noreferrer">
          ${icon("link")}<span>${REPOSITORY_LABEL}</span>
        </a>
      </div>
    `;
  }

  private renderVersion(info: SystemInfo): string {
    const server = info.server;
    const latest = server.latest;

    let badge = "";
    if (server.update_available === true) {
      badge = `<span class="profile-badge warn">Update available</span>`;
    } else if (server.update_available === false) {
      badge = `<span class="profile-badge ok">Up to date</span>`;
    } else if (server.source === "crate") {
      badge = `<span class="profile-badge">Dev build</span>`;
    }

    const latestValue = latest
      ? `<a class="profile-inline-link" href="${latest.url}" target="_blank" rel="noopener noreferrer">${escapeHtml(latest.tag)}</a>`
      : `<span class="profile-muted" title="${escapeHtml(server.latest_error || "")}">${server.latest_error ? "Unavailable" : "—"}</span>`;

    const download = latest?.matching_asset
      ? `<span class="mono">${escapeHtml(latest.matching_asset.name)}</span> <span class="profile-muted">${formatBytes(latest.matching_asset.size)}</span>`
      : "—";

    return `
      <section class="profile-section">
        <div class="profile-section-title">${icon("tag")}<span>Versioning</span></div>
        <div class="profile-card">
          ${row("Running binary", `<span class="mono">v${escapeHtml(server.version)}</span> ${badge}`)}
          ${row("Latest release", latestValue)}
          ${row("Published", latest ? formatDate(latest.published_at) : "—")}
          ${row("Download for this machine", download)}
          ${server.commit ? row("Commit", `<span class="mono">${escapeHtml(server.commit.slice(0, 12))}</span>`) : ""}
        </div>
      </section>
    `;
  }

  private renderRuntime(info: SystemInfo): string {
    const runtime = info.runtime;
    return `
      <section class="profile-section">
        <div class="profile-section-title">${icon("server")}<span>Runtime</span></div>
        <div class="profile-card">
          ${row("Package", `<span class="mono">${escapeHtml(runtime.package)}</span>`)}
          ${row("Installed version", runtime.version ? `<span class="mono">${escapeHtml(runtime.version)}</span>` : "—")}
          ${row("Python", runtime.python ? `<span class="mono">${escapeHtml(runtime.python)}</span>` : "—")}
          ${row("Location", runtime.location ? `<span class="mono profile-path">${escapeHtml(runtime.location)}</span>` : "—")}
        </div>
      </section>
    `;
  }

  private renderSandbox(info: SystemInfo): string {
    const sandbox = info.sandbox;
    const cores = sandbox.cpu_cores;
    const quota = sandbox.cpu_quota_cores;

    let coresValue = cores === null ? "—" : `${cores} ${cores === 1 ? "core" : "cores"}`;
    if (quota !== null) {
      coresValue += ` <span class="profile-muted">(${quota.toFixed(1)} CPU quota)</span>`;
    }

    const usedPct = percentOf(sandbox.memory_used_bytes, sandbox.memory_limit_bytes);
    const meter =
      usedPct === null
        ? ""
        : `<div class="profile-meter" title="${usedPct.toFixed(0)}% of limit">
             <div class="profile-meter-fill${usedPct > 90 ? " critical" : usedPct > 75 ? " high" : ""}" style="width:${usedPct.toFixed(1)}%"></div>
           </div>`;

    const memoryValue = `${formatBytes(sandbox.memory_used_bytes)} <span class="profile-muted">of ${formatBytes(sandbox.memory_limit_bytes)}</span>`;

    const notes: string[] = [];
    if (sandbox.memory_source === "cgroup") notes.push("limits read from this sandbox's cgroup");
    else if (sandbox.memory_source === "proc") notes.push("limits read from /proc/meminfo (host-wide)");
    if (sandbox.cpuset) notes.push(`cpuset ${escapeHtml(sandbox.cpuset)}`);
    if (sandbox.host_memory_total_bytes !== null && sandbox.memory_source === "cgroup") {
      notes.push(`host has ${formatBytes(sandbox.host_memory_total_bytes)}`);
    }

    return `
      <section class="profile-section">
        <div class="profile-section-title">${icon("cpu")}<span>Sandbox</span></div>
        <div class="profile-card">
          ${row("CPU", sandbox.cpu_model ? escapeHtml(sandbox.cpu_model) : "—")}
          ${row("Cores", coresValue)}
          ${row("Memory used", memoryValue)}
          ${meter}
          ${row("Memory free", formatBytes(sandbox.memory_free_bytes))}
          ${row("Peak usage", formatBytes(sandbox.memory_peak_bytes))}
          ${row("Workspace", `<span class="mono profile-path">${escapeHtml(sandbox.workspace_path)}</span>`)}
          ${notes.length ? `<div class="profile-note">${notes.join(" · ")}</div>` : ""}
        </div>
      </section>
    `;
  }

  private renderEnvironment(info: SystemInfo): string {
    if (info.environment.length === 0) {
      return `
        <section class="profile-section profile-section-wide">
          <div class="profile-section-title">${icon("sliders")}<span>Environment</span></div>
          <div class="profile-card">${row("Variables", "—")}</div>
        </section>
      `;
    }

    const rows = info.environment
      .map(
        (item) => `<div class="profile-row">
          <span class="profile-key mono">${escapeHtml(item.key)}</span>
          <span class="profile-value mono">${escapeHtml(item.value)}</span>
        </div>`
      )
      .join("");

    return `
      <section class="profile-section profile-section-wide">
        <div class="profile-section-title">${icon("sliders")}<span>Environment</span></div>
        <div class="profile-card">${rows}</div>
      </section>
    `;
  }
}
