import { agentSocket } from "../../core/agentSocket";
import {
  MODEL_CATALOG,
  PROVIDER_API_KEY_ENV,
  PROVIDER_LABELS,
  type AgentSettingsPatch,
  type AgentSettingsData,
  type McpServerDef,
} from "../../core/types";
import { editorSettings } from "../../state/store";
import { icon } from "../icons";

const ALL_TOOLS = [
  { name: "write_file", label: "Write File", desc: "Create or overwrite files", permissible: true },
  { name: "edit_file", label: "Edit File", desc: "Inline file edits", permissible: true },
  { name: "view_file", label: "View File", desc: "Read file contents", permissible: false },
  { name: "execute", label: "Execute", desc: "Run shell commands", permissible: true },
  { name: "read_output", label: "Read Output", desc: "Read terminal output", permissible: false },
  { name: "send_input", label: "Send Input", desc: "Send input to a terminal", permissible: false },
  { name: "terminate_terminal", label: "Terminate Terminal", desc: "Kill a terminal session", permissible: false },
  { name: "ask_user", label: "Ask User", desc: "Ask the user a question", permissible: false },
  { name: "find", label: "Find Files", desc: "Glob/grep search", permissible: false },
  { name: "semantic_search", label: "Semantic Search", desc: "Embedding-based code search", permissible: false },
  { name: "search_web", label: "Web Search", desc: "Search the internet", permissible: true },
];

type SettingsTab = "editor" | "model" | "tools" | "embedding" | "mcp" | "advanced";

/**
 * All the fields the form can edit but the server never round-trips back
 * (secrets, plus the freeform custom-instructions draft). Kept separate
 * from AgentSettingsData so that type still mirrors the server response
 * shape exactly.
 */
interface Draft {
  apiKey: string;
  embApiKey: string;
}

export class SettingsPanel {
  private overlay: HTMLElement;
  private nav: HTMLElement;
  private body: HTMLElement;
  private footerStatus: HTMLElement;

  private tab: SettingsTab = "editor";
  private settingsData: AgentSettingsData | null = null;
  private requestedSettings = false;
  private draft: Draft = { apiKey: "", embApiKey: "" };
  private openFileHandler: ((path: string) => void | Promise<void>) | null = null;

  constructor(parent: HTMLElement) {
    this.overlay = document.createElement("div");
    this.overlay.className = "settings-overlay";
    this.overlay.hidden = true;

    this.overlay.innerHTML = `
      <div class="settings-modal">
        <div class="settings-nav"></div>
        <div class="settings-body">
          <div class="settings-header">
            <span>Settings</span>
            <button class="icon-btn" data-close>${icon("close")}</button>
          </div>
          <div class="settings-content"></div>
          <div class="settings-footer">
            <span class="save-status"></span>
            <button class="btn" data-cancel>Cancel</button>
            <button class="btn primary" data-save>Save &amp; Restart</button>
          </div>
        </div>
      </div>
    `;
    parent.appendChild(this.overlay);

    this.nav = this.overlay.querySelector(".settings-nav")!;
    this.body = this.overlay.querySelector(".settings-content")!;
    this.footerStatus = this.overlay.querySelector(".save-status")!;

    this.overlay.querySelector("[data-close]")!.addEventListener("click", () => this.close());
    this.overlay.querySelector("[data-cancel]")!.addEventListener("click", () => this.close());
    this.overlay.querySelector("[data-save]")!.addEventListener("click", () => this.save());
    this.overlay.addEventListener("click", (e) => {
      if (e.target === this.overlay) this.close();
    });

    agentSocket.onMessage((m) => {
      if (m.type === "settings_data") {
        this.settingsData = (m as any).settings;
        this.draft = { apiKey: "", embApiKey: "" };
        this.renderTab();
      }
      if (m.type === "settings_updated") {
        this.footerStatus.textContent = m.success ? "Saved ✓" : `Error: ${m.message}`;
        this.footerStatus.className = "save-status " + (m.success ? "ok" : "err");
        if (m.success) setTimeout(() => this.close(), 800);
      }
    });
  }

  /** Lets main.ts wire "Open in Editor" without a circular import. */
  setOpenFileHandler(fn: (path: string) => void | Promise<void>) {
    this.openFileHandler = fn;
  }

  open() {
    this.overlay.hidden = false;
    this.footerStatus.textContent = "";
    this.footerStatus.className = "save-status";
    if (!this.requestedSettings) {
      agentSocket.send({ type: "event", event_type: "get_settings" });
      this.requestedSettings = true;
    }
    this.renderNav();
    this.renderTab();
  }

  close() {
    this.overlay.hidden = true;
    this.requestedSettings = false;
  }

  // ----------------------------------------------------------------- nav

  private renderNav() {
    const tabs: [SettingsTab, string, string][] = [
      ["editor", "Editor", "moon"],
      ["model", "Model", "cpu"],
      ["tools", "Tools", "wrench"],
      ["embedding", "Embedding", "search"],
      ["mcp", "MCP Servers", "server"],
      ["advanced", "Advanced", "sliders"],
    ];
    this.nav.innerHTML = tabs
      .map(
        ([id, label, ic]) =>
          `<div class="settings-nav-item ${id === this.tab ? "active" : ""}" data-tab="${id}">
             ${icon(ic as any)} ${label}
           </div>`
      )
      .join("");
    this.nav.querySelectorAll("[data-tab]").forEach((el) =>
      el.addEventListener("click", () => {
        // Every tab's controls write straight back into this.settingsData /
        // this.draft as they change, so switching tabs (which tears down
        // and rebuilds this.body) never loses anything — save() reads
        // from that persistent state, never from whichever tab happens to
        // be mounted at the moment Save is clicked.
        this.tab = el.getAttribute("data-tab")! as SettingsTab;
        this.renderNav();
        this.renderTab();
      })
    );
  }

  // ---------------------------------------------------------------- tabs

  private renderTab() {
    this.body.innerHTML = "";
    switch (this.tab) {
      case "editor": return this.renderEditor();
      case "model": return this.renderModel();
      case "tools": return this.renderTools();
      case "embedding": return this.renderEmbedding();
      case "mcp": return this.renderMcp();
      case "advanced": return this.renderAdvanced();
    }
  }

  // ---- Editor ----
  // (These commit straight to the localStorage-backed editorSettings store
  // on change, so they're not part of the agent.yaml patch / cross-tab
  // persistence story below.)

  private renderEditor() {
    const s = editorSettings.get();
    this.body.innerHTML = `
      <div class="field-group">
        <div class="group-title">Appearance</div>
        ${this.themeRow(s.theme)}
        ${this.numRow("font-size", "Font Size", "Editor font size in pixels", s.fontSize, 8, 32)}
        ${this.numRow("tab-size", "Tab Size", "Spaces per tab", s.tabSize, 1, 8)}
      </div>
      <div class="field-group">
        <div class="group-title">Behavior</div>
        ${this.switchRow("word-wrap", "Word Wrap", "Wrap long lines", s.wordWrap)}
        ${this.switchRow("minimap", "Minimap", "Show scrollbar overview", s.minimap)}
        ${this.switchRow("format-save", "Format on Save", "Auto-format when saving", s.formatOnSave)}
      </div>
    `;
    this.bindEditorEvents();
  }

  private themeRow(current: "dark" | "light") {
    return `
      <div class="field-row">
        <div class="label"><span class="t">Theme</span><span class="d">Editor and UI color scheme</span></div>
        <div class="control">
          <div class="theme-toggle">
            <button class="theme-opt ${current === "dark" ? "active" : ""}" data-theme="dark">${icon("moon")} Dark</button>
            <button class="theme-opt ${current === "light" ? "active" : ""}" data-theme="light">${icon("sun")} Light</button>
          </div>
        </div>
      </div>`;
  }

  private numRow(id: string, label: string, desc: string, value: number, min: number, max: number) {
    return `
      <div class="field-row">
        <div class="label"><span class="t">${label}</span><span class="d">${desc}</span></div>
        <div class="control"><input type="number" data-field="${id}" value="${value}" min="${min}" max="${max}" style="width:70px"/></div>
      </div>`;
  }

  private switchRow(id: string, label: string, desc: string, on: boolean) {
    return `
      <div class="field-row">
        <div class="label"><span class="t">${label}</span><span class="d">${desc}</span></div>
        <div class="control"><div class="switch ${on ? "on" : ""}" data-field="${id}"></div></div>
      </div>`;
  }

  private bindEditorEvents() {
    this.body.querySelectorAll("[data-theme]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const theme = btn.getAttribute("data-theme") as "dark" | "light";
        editorSettings.update((s) => ({ ...s, theme }));
        this.renderTab();
      })
    );
    this.body.querySelectorAll('input[type="number"]').forEach((inp) =>
      inp.addEventListener("change", () => {
        const field = (inp as HTMLInputElement).dataset.field!;
        const val = parseInt((inp as HTMLInputElement).value, 10);
        if (isNaN(val)) return;
        editorSettings.update((s) => ({
          ...s,
          ...(field === "font-size" && { fontSize: val }),
          ...(field === "tab-size" && { tabSize: val }),
        }));
      })
    );
    this.body.querySelectorAll(".switch").forEach((sw) =>
      sw.addEventListener("click", () => {
        const field = (sw as HTMLElement).dataset.field!;
        sw.classList.toggle("on");
        const on = sw.classList.contains("on");
        editorSettings.update((s) => ({
          ...s,
          ...(field === "word-wrap" && { wordWrap: on }),
          ...(field === "minimap" && { minimap: on }),
          ...(field === "format-save" && { formatOnSave: on }),
        }));
      })
    );
  }

  // ---- Model ----

  private renderModel() {
    const d = this.settingsData;
    const provider = d?.provider || "experientiallabs";
    const models = MODEL_CATALOG[provider] || [];
    const currentModel = models.find((m) => m.name === d?.model) || models[0];
    const thinkingEnabled = d?.thinking?.enabled ?? false;

    this.body.innerHTML = `
      <div class="field-group">
        <div class="group-title">LLM Provider ${this.infoTip("Select the AI provider. Each provider requires its own API key.")}</div>
        ${this.selectRow("provider", "Provider", "AI model provider", Object.keys(MODEL_CATALOG).map((k) => [k, PROVIDER_LABELS[k] || k]), provider)}
        ${this.selectRow("model", "Model", "Specific model to use", models.map((m) => [m.name, `${m.name} (${m.notes})`]), d?.model || "")}
      </div>
      <div class="field-group">
        <div class="group-title">Parameters ${this.infoTip("Temperature: higher = more creative, lower = more deterministic. Max tokens: maximum output length.")}</div>
        ${this.numRow2("temperature", "Temperature", "0.0–2.0", d?.temperature ?? 1.0, 0, 2, 0.1)}
        ${this.numRow2("max-tokens", "Max Tokens", "Maximum output tokens", d?.max_tokens ?? 65536, 1, 384000, 1)}
      </div>
      <div class="field-group">
        <div class="group-title">Authentication ${this.infoTip("Environment variable where the API key is stored. Provide your key below if using your own.")}</div>
        ${this.textRow("api-key-env", "API Key Env Var", "e.g. DASHSCOPE_API_KEY", d?.api_key_env || PROVIDER_API_KEY_ENV[provider] || "")}
        ${this.textRow("api-key", "API Key", "Your API key (optional, uses platform default if empty)", this.draft.apiKey, "password")}
      </div>
      <div class="field-group">
        <div class="group-title">Thinking / Reasoning ${this.infoTip("Enable extended reasoning. Budget tokens control thinking depth. Reasoning effort controls how thoroughly the model reasons.")}</div>
        ${this.switchRow("thinking-enabled", "Enable Thinking", currentModel ? `Mode: ${currentModel.thinkingMode}` : "", thinkingEnabled)}
        ${currentModel?.reasoningEffortSupport && thinkingEnabled ? this.selectRow(
          "reasoning-effort",
          "Reasoning Effort",
          "How deeply the model should reason",
          currentModel.reasoningEffortLevels.map((l) => [l, l.charAt(0).toUpperCase() + l.slice(1)]),
          d?.thinking?.reasoning_effort || "high"
        ) : ""}
        ${thinkingEnabled ? this.numRow2("budget-tokens", "Budget Tokens", "Max thinking tokens", d?.thinking?.budget_tokens ?? 32000, 1000, 200000, 1) : ""}
      </div>
      ${currentModel ? `<div class="model-info-card">
        <div style="font-size:11px;color:var(--fg-2);margin-bottom:4px">Model Info</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px;color:var(--fg-1)">
          <span>Context Window</span><span>${(currentModel.contextWindow/1000).toFixed(0)}K tokens</span>
          <span>Max Output</span><span>${(currentModel.maxOutputTokens/1000).toFixed(0)}K tokens</span>
          <span>Pricing</span><span>$${currentModel.pricing.input}/$${currentModel.pricing.output} per 1M tokens</span>
        </div>
      </div>` : ""}
    `;
    this.bindModelEvents();
  }

  private selectRow(id: string, label: string, desc: string, options: [string, string][], current: string) {
    const opts = options.map(([val, lbl]) => `<option value="${val}" ${val === current ? "selected" : ""}>${escapeHtml(lbl)}</option>`).join("");
    return `
      <div class="field-row">
        <div class="label"><span class="t">${label}</span><span class="d">${desc}</span></div>
        <div class="control"><select data-field="${id}">${opts}</select></div>
      </div>`;
  }

  private textRow(id: string, label: string, placeholder: string, value: string, type = "text") {
    return `
      <div class="field-row">
        <div class="label"><span class="t">${label}</span></div>
        <div class="control"><input type="${type}" data-field="${id}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" style="min-width:200px"/></div>
      </div>`;
  }

  /** Like numRow but with an explicit step and live change-tracking wired by the caller. */
  private numRow2(id: string, label: string, desc: string, value: number, min: number, max: number, step: number) {
    return `
      <div class="field-row">
        <div class="label"><span class="t">${label}</span><span class="d">${desc}</span></div>
        <div class="control"><input type="number" data-field="${id}" value="${value}" min="${min}" max="${max}" step="${step}" style="width:100px"/></div>
      </div>`;
  }

  private infoTip(text: string) {
    return `<span class="info-tip" title="${escapeHtml(text)}">${icon("info")}</span>`;
  }

  private bindModelEvents() {
    const d = this.settingsData;
    if (!d) return;

    const providerSel = this.body.querySelector('[data-field="provider"]') as HTMLSelectElement | null;
    providerSel?.addEventListener("change", () => {
      d.provider = providerSel.value;
      const models = MODEL_CATALOG[providerSel.value] || [];
      d.model = models[0]?.name || "";
      d.api_key_env = PROVIDER_API_KEY_ENV[providerSel.value] || "";
      this.renderTab();
    });

    const modelSel = this.body.querySelector('[data-field="model"]') as HTMLSelectElement | null;
    modelSel?.addEventListener("change", () => { d.model = modelSel.value; this.renderTab(); });

    this.onNum("temperature", (v) => (d.temperature = v));
    this.onNum("max-tokens", (v) => (d.max_tokens = v));
    this.onText("api-key-env", (v) => (d.api_key_env = v));
    this.onText("api-key", (v) => (this.draft.apiKey = v));

    const thinkingSwitch = this.body.querySelector('[data-field="thinking-enabled"]') as HTMLElement | null;
    thinkingSwitch?.addEventListener("click", () => {
      thinkingSwitch.classList.toggle("on");
      d.thinking.enabled = thinkingSwitch.classList.contains("on");
      this.renderTab();
    });

    const effortSel = this.body.querySelector('[data-field="reasoning-effort"]') as HTMLSelectElement | null;
    effortSel?.addEventListener("change", () => (d.thinking.reasoning_effort = effortSel.value));
    this.onNum("budget-tokens", (v) => (d.thinking.budget_tokens = v));
  }

  // ---- shared binding helpers ----
  // Every field on every tab wires through one of these so a value survives
  // switching tabs (which rebuilds this.body from scratch each time).

  private onText(field: string, apply: (v: string) => void) {
    const el = this.body.querySelector(`[data-field="${field}"]`) as HTMLInputElement | null;
    el?.addEventListener("input", () => apply(el.value));
  }

  private onNum(field: string, apply: (v: number) => void) {
    const el = this.body.querySelector(`[data-field="${field}"]`) as HTMLInputElement | null;
    el?.addEventListener("input", () => {
      const n = parseFloat(el.value);
      if (!isNaN(n)) apply(n);
    });
  }

  private onSwitch(field: string, apply: (v: boolean) => void) {
    const el = this.body.querySelector(`[data-field="${field}"].switch`) as HTMLElement | null;
    el?.addEventListener("click", () => {
      el.classList.toggle("on");
      apply(el.classList.contains("on"));
    });
  }

  // ---- Tools ----

  private renderTools() {
    const d = this.settingsData;
    const toolStates: Record<string, { enabled: boolean; require_permission: boolean }> = {};
    for (const t of ALL_TOOLS) {
      const serverTool = d?.tools?.find((st) => st.name === t.name);
      toolStates[t.name] = {
        enabled: serverTool?.enabled ?? true,
        require_permission: serverTool?.require_permission ?? false,
      };
    }

    this.body.innerHTML = `
      <div class="field-group">
        <div class="group-title">Agent Tools ${this.infoTip("Enable or disable individual tools the agent can use. 'Permission' tools require user approval before executing.")}</div>
        <div class="tool-toggle-list">
          ${ALL_TOOLS.map((t) => {
            const st = toolStates[t.name];
            return `
              <div class="tool-toggle-row">
                <div class="switch ${st.enabled ? "on" : ""}" data-tool="${t.name}" data-action="toggle"></div>
                <div class="tool-toggle-info">
                  <span class="tool-toggle-name">${t.label}</span>
                  <span class="tool-toggle-desc">${t.desc}</span>
                </div>
                ${t.permissible ? `
                  <div class="tool-perm">
                    <label class="tool-perm-label">
                      <input type="checkbox" data-tool-perm="${t.name}" ${st.require_permission ? "checked" : ""} />
                      <span>Require permission</span>
                    </label>
                  </div>
                ` : ""}
              </div>
            `;
          }).join("")}
        </div>
      </div>
    `;

    const writeBack = (name: string, patch: Partial<{ enabled: boolean; require_permission: boolean }>) => {
      if (!d) return;
      if (!d.tools) d.tools = [];
      let entry = d.tools.find((t) => t.name === name);
      if (!entry) {
        entry = { name, enabled: true, require_permission: false };
        d.tools.push(entry);
      }
      Object.assign(entry, patch);
    };

    this.body.querySelectorAll('[data-action="toggle"]').forEach((sw) =>
      sw.addEventListener("click", () => {
        sw.classList.toggle("on");
        writeBack((sw as HTMLElement).dataset.tool!, { enabled: sw.classList.contains("on") });
      })
    );
    this.body.querySelectorAll("[data-tool-perm]").forEach((cb) =>
      cb.addEventListener("change", () => {
        const input = cb as HTMLInputElement;
        writeBack(input.dataset.toolPerm!, { require_permission: input.checked });
      })
    );
  }

  // ---- Embedding ----

  private renderEmbedding() {
    const d = this.settingsData;
    const e = d?.embedding;
    this.body.innerHTML = `
      <div class="field-group">
        <div class="group-title">Embedding Model ${this.infoTip("Used by Semantic Search and MCP tools for code similarity search. Requires a separate API key from the LLM provider.")}</div>
        ${this.textRow("emb-model", "Model Name", "e.g. voyage-code-3", e?.model || "voyage-code-3")}
        ${this.textRow("emb-base-url", "Base URL", "API endpoint", e?.base_url || "https://api.voyageai.com/v1")}
        ${this.textRow("emb-api-key-env", "API Key Env Var", "e.g. VOYAGE_API_KEY", e?.api_key_env || "VOYAGE_API_KEY")}
        ${this.textRow("emb-api-key", "Embedding API Key", "Your embedding API key (optional)", this.draft.embApiKey, "password")}
      </div>
      <div class="field-group">
        <div class="group-title">Features</div>
        ${this.switchRow("semantic-search", "Semantic Search", "Enable embedding-based code search tool", d?.semantic_search_enabled ?? true)}
      </div>
    `;
    if (!d) return;
    if (!d.embedding) d.embedding = { model: "voyage-code-3", base_url: "https://api.voyageai.com/v1", api_key_env: "VOYAGE_API_KEY" };
    this.onText("emb-model", (v) => (d.embedding.model = v));
    this.onText("emb-base-url", (v) => (d.embedding.base_url = v));
    this.onText("emb-api-key-env", (v) => (d.embedding.api_key_env = v));
    this.onText("emb-api-key", (v) => (this.draft.embApiKey = v));
    this.onSwitch("semantic-search", (v) => (d.semantic_search_enabled = v));
  }

  // ---- MCP ----

  private renderMcp() {
    const d = this.settingsData;
    const mcpEnabled = d?.mcp_enabled ?? false;
    const servers = d?.mcp_servers || [];

    this.body.innerHTML = `
      <div class="field-group">
        <div class="group-title">MCP (Model Context Protocol) ${this.infoTip("Connect to external tool servers via MCP. Each server provides additional capabilities to the agent.")}</div>
        ${this.switchRow("mcp-enabled", "Enable MCP", "Allow agent to use MCP tool servers", mcpEnabled)}
      </div>
      ${mcpEnabled ? `
        <div class="field-group">
          <div class="group-title">MCP Servers</div>
          <div class="mcp-server-list" id="mcp-servers">
            ${servers.length ? servers.map((srv, i) => this.renderMcpServer(srv, i)).join("") : `<div class="mcp-empty">No servers added yet.</div>`}
          </div>
          <button class="btn" id="add-mcp-server" style="margin-top:8px">${icon("plus")} Add Server</button>
        </div>
      ` : ""}
    `;
    if (!d) return;

    this.onSwitch("mcp-enabled", (v) => (d.mcp_enabled = v));

    this.body.querySelector("#add-mcp-server")?.addEventListener("click", () => {
      d.mcp_servers = [...(d.mcp_servers || []), { name: "", url: "", api_key_env: "", api_key_param: "", api_key: "" }];
      this.renderTab();
    });

    this.body.querySelectorAll("[data-mcp-field]").forEach((el) => {
      el.addEventListener("input", () => {
        const inp = el as HTMLInputElement;
        const idx = parseInt(inp.dataset.mcpIdx!, 10);
        const key = inp.dataset.mcpField! as keyof McpServerDef;
        const srv = d.mcp_servers?.[idx];
        if (srv) (srv as any)[key] = inp.value;
      });
    });

    this.body.querySelectorAll("[data-remove-server]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const idx = parseInt((btn as HTMLElement).dataset.removeServer!, 10);
        d.mcp_servers = (d.mcp_servers || []).filter((_, i) => i !== idx);
        this.renderTab();
      })
    );
  }

  private renderMcpServer(srv: McpServerDef, idx: number) {
    return `
      <div class="mcp-server-card">
        <div class="mcp-server-header">
          <span class="mcp-server-title">${icon("server")} ${escapeHtml(srv.name) || `Server ${idx + 1}`}</span>
          <button class="icon-btn" data-remove-server="${idx}" title="Remove server">${icon("trash")}</button>
        </div>
        <div class="mcp-server-fields">
          <div class="field-row">
            <div class="label"><span class="t">Name</span></div>
            <div class="control"><input type="text" data-mcp-field="name" data-mcp-idx="${idx}" placeholder="tavily-mcp" value="${escapeHtml(srv.name)}" /></div>
          </div>
          <div class="field-row">
            <div class="label"><span class="t">Server URL</span></div>
            <div class="control"><input type="text" data-mcp-field="url" data-mcp-idx="${idx}" placeholder="https://mcp.tavily.com/mcp/" value="${escapeHtml(srv.url)}" /></div>
          </div>
          <div class="field-row">
            <div class="label"><span class="t">API Key / Auth Token</span><span class="d">Sent to the runtime, exported under the env var below, then scrubbed right after startup — never written to agent.yaml</span></div>
            <div class="control"><input type="password" data-mcp-field="api_key" data-mcp-idx="${idx}" placeholder="Paste the secret value" value="${escapeHtml(srv.api_key || "")}" /></div>
          </div>
          <div class="field-row">
            <div class="label"><span class="t">API Key Env Var</span><span class="d">The env var name the key above gets exported as (this name — not the secret — is what's saved to agent.yaml)</span></div>
            <div class="control"><input type="text" data-mcp-field="api_key_env" data-mcp-idx="${idx}" placeholder="TAVILY_API_KEY" value="${escapeHtml(srv.api_key_env)}" /></div>
          </div>
          <div class="field-row">
            <div class="label"><span class="t">Key Param Name</span><span class="d">Header name (e.g. Authorization, X-Api-Key) or query parameter name (e.g. tavilyApiKey) — headers are auto-detected by name</span></div>
            <div class="control"><input type="text" data-mcp-field="api_key_param" data-mcp-idx="${idx}" placeholder="tavilyApiKey" value="${escapeHtml(srv.api_key_param)}" /></div>
          </div>
        </div>
      </div>
    `;
  }

  // ---- Advanced ----

  private renderAdvanced() {
    const d = this.settingsData;
    const mode = d?.system_prompt_mode ?? "inline";
    this.body.innerHTML = `
      <div class="field-group">
        <div class="group-title">Runtime ${this.infoTip("Max steps: limits agent tool-call loops. Unsafe mode: skips all permission prompts.")}</div>
        ${this.numRow2("max-steps", "Max Steps", "Maximum agent steps per task", d?.max_steps ?? 35, 1, 200, 1)}
        ${this.switchRow("unsafe-mode", "Unsafe Mode", "Skip all tool permission prompts (⚠️ use with caution)", d?.unsafe_mode ?? true)}
      </div>
      <div class="field-group">
        <div class="group-title">Memory Management ${this.infoTip("Control context maintenance thresholds.")}</div>
        ${this.numRow2("max-context-tokens", "Max Context Tokens", "Max context size", d?.memory?.max_context_tokens ?? 120000, 1000, 1000000, 1000)}
        ${this.numRow2("context-stress-multiplier", "Stress Multiplier", "Multiplier for tokens", d?.memory?.context_stress_multiplier ?? 1.0, 0.1, 5.0, 0.1)}
        ${this.numRow2("context-stress-trigger", "Stress Trigger", "Trigger threshold (0.0 to 1.0)", d?.memory?.context_stress_trigger ?? 0.78, 0.1, 1.0, 0.01)}
      </div>
      <div class="field-group">
        <div class="group-title">Sub-Agents ${this.infoTip("Enable the agent to spawn sub-agents for parallel work.")}</div>
        ${this.switchRow("sub-agents-enabled", "Enable Sub-Agents", "Allow spawning sub-agents", d?.sub_agents?.enabled ?? false)}
        ${this.numRow2("sub-agents-steps", "Sub-Agent Max Steps", "Step limit for each sub-agent", d?.sub_agents?.max_steps ?? 20, 1, 100, 1)}
      </div>
      <div class="field-group">
        <div class="group-title">System Prompt ${this.infoTip("Inline: your text is appended to the fixed default prompt. File: the agent reads its system prompt from a Markdown file on disk, so you can edit it directly and it takes effect on the next Save & Restart or new session.")}</div>
        <div class="field-row">
          <div class="label"><span class="t">Source</span><span class="d">Where the agent's custom instructions come from</span></div>
          <div class="control">
            <div class="theme-toggle">
              <button class="theme-opt ${mode === "inline" ? "active" : ""}" data-prompt-mode="inline">Inline text</button>
              <button class="theme-opt ${mode === "file" ? "active" : ""}" data-prompt-mode="file">instructions.md</button>
            </div>
          </div>
        </div>
        ${mode === "inline" ? `
          <div class="field-row full">
            <div class="label"><span class="t">Custom Instructions</span><span class="d">Appended after the default system prompt — replaces cleanly on every save, never duplicates</span></div>
            <div class="control">
              <textarea data-field="system-prompt" rows="4" placeholder="Add custom instructions for the agent…">${escapeHtml(d?.system_prompt_custom || "")}</textarea>
            </div>
          </div>
        ` : `
          <div class="field-row full">
            <div class="label"><span class="t">Instructions File</span><span class="d">${escapeHtml(d?.instructions_file_path || "./prompts/instructions.md")}</span></div>
            <div class="control">
              <button class="btn" id="open-instructions-file">${icon("fileIcon")} Open in Editor</button>
            </div>
          </div>
        `}
      </div>
    `;
    if (!d) return;

    this.onNum("max-steps", (v) => (d.max_steps = v));
    this.onSwitch("unsafe-mode", (v) => (d.unsafe_mode = v));

    if (!d.memory) d.memory = { max_context_tokens: 120000, context_stress_multiplier: 1.0, context_stress_trigger: 0.78 };
    this.onNum("max-context-tokens", (v) => (d.memory.max_context_tokens = v));
    this.onNum("context-stress-multiplier", (v) => (d.memory.context_stress_multiplier = v));
    this.onNum("context-stress-trigger", (v) => (d.memory.context_stress_trigger = v));

    if (!d.sub_agents) d.sub_agents = { enabled: false, max_steps: 20 };
    this.onSwitch("sub-agents-enabled", (v) => (d.sub_agents.enabled = v));
    this.onNum("sub-agents-steps", (v) => (d.sub_agents.max_steps = v));

    this.body.querySelectorAll("[data-prompt-mode]").forEach((btn) =>
      btn.addEventListener("click", () => {
        d.system_prompt_mode = btn.getAttribute("data-prompt-mode") as "inline" | "file";
        this.renderTab();
      })
    );

    this.onText("system-prompt", (v) => (d.system_prompt_custom = v));

    this.body.querySelector("#open-instructions-file")?.addEventListener("click", () => {
      if (d.instructions_file_path && this.openFileHandler) {
        this.close();
        Promise.resolve(this.openFileHandler(d.instructions_file_path)).catch((e) => {
          console.error("failed to open instructions file", e);
        });
      }
    });
  }

  // ---------------------------------------------------------------- save

  private save() {
    this.footerStatus.textContent = "Saving…";
    this.footerStatus.className = "save-status";

    const d = this.settingsData;
    if (!d) return;

    const patch: AgentSettingsPatch = {
      provider: d.provider,
      model: d.model,
      temperature: d.temperature,
      max_tokens: d.max_tokens,
      api_key_env: d.api_key_env,
      thinking: {
        enabled: d.thinking.enabled,
        reasoning_effort: d.thinking.reasoning_effort,
        budget_tokens: d.thinking.budget_tokens,
      },
      max_steps: d.max_steps,
      unsafe_mode: d.unsafe_mode,
      sub_agents: { enabled: d.sub_agents?.enabled ?? false, max_steps: d.sub_agents?.max_steps ?? 20 },
      memory: {
        max_context_tokens: d.memory?.max_context_tokens ?? 120000,
        context_stress_multiplier: d.memory?.context_stress_multiplier ?? 1.0,
        context_stress_trigger: d.memory?.context_stress_trigger ?? 0.78,
      },
      embedding: {
        model: d.embedding?.model || "voyage-code-3",
        base_url: d.embedding?.base_url || "https://api.voyageai.com/v1",
        api_key_env: d.embedding?.api_key_env || "VOYAGE_API_KEY",
        api_key: this.draft.embApiKey || undefined,
      },
      semantic_search_enabled: d.semantic_search_enabled,
      system_prompt_mode: d.system_prompt_mode,
      system_prompt_append: d.system_prompt_mode === "inline" ? (d.system_prompt_custom || "") : "",
    };

    if (this.draft.apiKey) patch.api_key = this.draft.apiKey;

    if (d.tools?.length) {
      patch.tools = {};
      for (const t of d.tools) {
        patch.tools[t.name] = { enabled: t.enabled, require_permission: t.require_permission };
      }
    }

    const servers: McpServerDef[] = (d.mcp_servers || [])
      .filter((s) => s.url.trim())
      .map((s) => ({
        name: s.name.trim(),
        url: s.url.trim(),
        api_key_env: s.api_key_env.trim(),
        api_key_param: s.api_key_param.trim(),
        api_key: s.api_key?.trim() || undefined,
      }));
    patch.mcp = {
      enabled: d.mcp_enabled ?? false,
      servers,
      embedding_model: d.embedding?.model,
      embedding_api_key_env: d.embedding?.api_key_env,
      embedding_base_url: d.embedding?.base_url,
      embedding_api_key: this.draft.embApiKey || undefined,
    };

    agentSocket.send({ type: "event", event_type: "update_settings", settings: patch });
  }
}

function escapeHtml(s: string): string {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}
