import { agentSocket } from "../../core/agentSocket";
import type { AgentInbound } from "../../core/types";
import { icon } from "../icons";
import { loadActiveSession, saveActiveSession } from "../../state/store";
import { marked } from "marked";
import DOMPurify from "dompurify";
import {
  activitySummary,
  enrichToolRecord,
  formatDuration,
  kindBreakdown,
  parseLegacyArguments,
  parseToolArguments,
  describeTool,
  type ToolRecord,
} from "./toolPresentation";
import { Odometer } from "./odometer";

type FileWrittenHandler = (path: string) => void;

/**
 * Agent chat panel.
 *
 * Layout contract:
 *   - The transcript is append-only. Tokens never re-render the whole list.
 *   - Tool activity lives in a dock that grows out of the composer, so the
 *     user's eye stays on the input while work happens.
 *   - When a turn ends, the same records collapse into a summary chip placed
 *     *after* the reply, so history stays readable minutes later.
 *
 * Both paths — live websocket events and replayed session files — feed the
 * same ToolRecord model, which is why a resumed conversation looks identical
 * to one that just ran.
 */
export class AgentPanel {
  private el: HTMLElement;
  private messagesEl: HTMLElement;
  private composer: HTMLTextAreaElement;
  private sendBtn: HTMLButtonElement;
  private abortBtn: HTMLButtonElement;
  private permissionBarEl: HTMLElement;

  // Streaming state for the current assistant block.
  private currentThinkingEl: HTMLElement | null = null;
  private currentThinkingBoxEl: HTMLElement | null = null;
  private currentTextEl: HTMLElement | null = null;
  private currentRawText = "";
  private currentStreamFrame: number | null = null;
  private inThinking = false;

  // Current turn.
  private currentTurnEl: HTMLElement | null = null;
  private turnRecords: ToolRecord[] = [];
  private liveRows = new Map<number, HTMLElement>();
  private recordSeq = 0;
  private lastActivity: ToolRecord | null = null;
  private turnStart = 0;
  private turnTimer: number | null = null;

  // Activity dock (grows out of the composer).
  private dockEl: HTMLElement;
  private dockListEl: HTMLElement;
  private dockTitleEl: HTMLElement;
  private dockSubEl: HTMLElement;
  private dockTimeEl: HTMLElement;
  private dockCountEl: HTMLElement;
  private dockUnitEl: HTMLElement;
  private odometer: Odometer;
  private dockOpen = false;
  private dockClearTimer: number | null = null;

  // Permission gate.
  private pendingPermission: { tool: string; description: string } | null = null;
  private permissionKeyHandler: ((e: KeyboardEvent) => void) | null = null;

  private activeSession: string;
  private sessionsList: any[] = [];
  private fileWrittenHandlers = new Set<FileWrittenHandler>();
  private stickToBottom = true;

  constructor(container: HTMLElement) {
    this.activeSession = loadActiveSession();

    this.el = document.createElement("div");
    this.el.className = "panel";
    this.el.innerHTML = `
      <div class="panel-header">
        <span>Agent</span>
        <div class="actions">
          <button class="icon-btn" data-act="sessions" title="History / Sessions">${icon("clock")}</button>
          <button class="icon-btn" data-act="new-session" title="New Session">${icon("plus")}</button>
        </div>
      </div>
      <div class="agent-messages"></div>
      <div class="agent-composer">
        <div class="activity-dock">
          <div class="activity-shell">
            <div class="activity-head" role="button" tabindex="0" aria-expanded="false">
              <span class="activity-orb"></span>
              <span class="activity-title">Working</span>
              <span class="activity-sub"></span>
              <span class="activity-meter">
                <span class="activity-count"></span>
                <span class="activity-unit">tools</span>
              </span>
              <span class="activity-time">0.0s</span>
              <span class="activity-chev">${icon("chevDown")}</span>
            </div>
            <div class="activity-list"></div>
          </div>
        </div>
        <div class="permission-bar"></div>
        <div class="composer-box">
          <textarea rows="1" placeholder="Message the agent…"></textarea>
          <div class="composer-actions">
            <button class="icon-btn abort-btn" title="Abort" style="display:none">${icon("stop")}</button>
            <button class="icon-btn send-btn" title="Send">${icon("send")}</button>
          </div>
        </div>
        <div class="composer-meta">
          <span>Enter to send · Shift+Enter for newline</span>
          <span data-conn>connecting…</span>
        </div>
      </div>
    `;
    container.appendChild(this.el);

    this.messagesEl = this.el.querySelector(".agent-messages")!;
    this.composer = this.el.querySelector("textarea")!;
    this.sendBtn = this.el.querySelector(".send-btn")!;
    this.abortBtn = this.el.querySelector(".abort-btn")!;
    this.permissionBarEl = this.el.querySelector(".permission-bar")!;

    this.dockEl = this.el.querySelector(".activity-dock")!;
    this.dockListEl = this.el.querySelector(".activity-list")!;
    this.dockTitleEl = this.el.querySelector(".activity-title")!;
    this.dockSubEl = this.el.querySelector(".activity-sub")!;
    this.dockTimeEl = this.el.querySelector(".activity-time")!;
    this.dockCountEl = this.el.querySelector(".activity-count")!;
    this.dockUnitEl = this.el.querySelector(".activity-unit")!;
    this.odometer = new Odometer(this.dockCountEl);

    this.renderEmptyState();

    this.el.querySelector('[data-act="new-session"]')!.addEventListener("click", () => this.newSession());
    this.el.querySelector('[data-act="sessions"]')!.addEventListener("click", () => {
      agentSocket.send({ type: "event", event_type: "list_sessions" });
    });

    this.dockEl.querySelector(".activity-head")!.addEventListener("click", () => this.toggleDock());
    this.dockEl.querySelector(".activity-head")!.addEventListener("keydown", (e) => {
      const key = (e as KeyboardEvent).key;
      if (key === "Enter" || key === " ") {
        e.preventDefault();
        this.toggleDock();
      }
    });

    this.abortBtn.addEventListener("click", () => this.abort());
    this.sendBtn.addEventListener("click", () => this.send());
    this.composer.addEventListener("keydown", (e) => {
      // Alt+Enter approves a pending permission from anywhere in the composer.
      if (this.pendingPermission && e.altKey && e.key === "Enter") {
        e.preventDefault();
        this.resolvePermission(true);
        return;
      }
      if (((e.metaKey || e.ctrlKey) && e.key === "Enter") || (e.key === "Enter" && !e.shiftKey && !e.altKey)) {
        e.preventDefault();
        this.send();
      }
    });
    this.composer.addEventListener("input", () => {
      this.composer.style.height = "auto";
      this.composer.style.height = Math.min(this.composer.scrollHeight, 160) + "px";
      this.sendBtn.disabled = this.composer.value.trim().length === 0;
    });
    this.sendBtn.disabled = true;

    this.messagesEl.addEventListener("scroll", () => {
      const distance = this.messagesEl.scrollHeight - this.messagesEl.scrollTop - this.messagesEl.clientHeight;
      this.stickToBottom = distance < 64;
    });

    agentSocket.onConnectionChange((c) => {
      const el = this.el.querySelector("[data-conn]")!;
      el.textContent = c ? "" : "reconnecting…";
    });
    agentSocket.onMessage((m) => this.handleInbound(m));
  }

  onFileWritten(h: FileWrittenHandler) {
    this.fileWrittenHandlers.add(h);
  }

  // --------------------------------------------------------------- sessions

  private renderSessionsModal() {
    const existing = document.getElementById("sessions-modal");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "sessions-modal";
    overlay.className = "modal-overlay";

    let listHtml = "";
    for (const s of this.sessionsList) {
      const isActive = s.session_id === this.activeSession;
      const date = new Date(s.updated_at * 1000).toLocaleString();
      listHtml += `
        <div class="session-card ${isActive ? "active" : ""}" data-id="${s.session_id}">
          <div class="session-card-header">
            <span class="session-card-title">${isActive ? '<span class="session-active-dot"></span>' : ""}${escapeHtml(s.session_id)}</span>
            <span class="session-card-date">${date}</span>
          </div>
          <div class="session-card-snippet">${escapeHtml(s.snippet)}</div>
          <button class="icon-btn delete-btn" title="Delete Session" data-id="${s.session_id}">${icon("trash")}</button>
        </div>
      `;
    }

    overlay.innerHTML = `
      <div class="modal sessions-modal">
        <div class="modal-header">
          <h3>Chat History</h3>
          <button class="icon-btn close-btn">${icon("close")}</button>
        </div>
        <div class="sessions-list">
          ${listHtml || '<div style="padding:20px;color:var(--fg-2);text-align:center;">No sessions found.</div>'}
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    overlay.querySelector(".close-btn")!.addEventListener("click", () => overlay.remove());
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) overlay.remove();
    });

    overlay.querySelectorAll(".session-card").forEach((card) => {
      card.addEventListener("click", (e) => {
        if ((e.target as HTMLElement).closest(".delete-btn")) return;
        const id = card.getAttribute("data-id")!;
        this.switchSession(id);
        overlay.remove();
      });
    });

    overlay.querySelectorAll(".delete-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = btn.getAttribute("data-id")!;
        if (confirm(`Are you sure you want to delete session ${id}?`)) {
          agentSocket.send({ type: "event", event_type: "delete_session", session_id: id });
          btn.closest(".session-card")?.remove();
        }
      });
    });
  }

  private newSession() {
    const id = `session_${Date.now()}`;
    agentSocket.send({ type: "event", event_type: "new_session", session_id: id });
  }

  private switchSession(id: string) {
    if (id === this.activeSession) return;
    agentSocket.send({ type: "event", event_type: "switch_session", session_id: id });
  }

  // ------------------------------------------------------------------- send

  private send() {
    const text = this.composer.value.trim();
    if (!text) return;

    // If a permission gate is open, typing + sending means "reject this
    // call, then immediately run what I typed".
    if (this.pendingPermission) this.resolvePermission(false);

    if (this.currentTurnEl) this.finalizeTurn();
    this.appendUserTurn(text, true);
    agentSocket.send({ type: "task", text });

    this.composer.value = "";
    this.composer.style.height = "auto";
    this.sendBtn.disabled = true;
    this.setRunning(true);
    this.scrollToBottom(true);
  }

  private abort() {
    agentSocket.send({ type: "event", event_type: "abort" });
    this.setRunning(false);
    this.finalizeTurn();
  }

  private setRunning(running: boolean) {
    this.abortBtn.style.display = running ? "flex" : "none";
  }

  // ---------------------------------------------------------------- inbound

  private handleInbound(msg: AgentInbound) {
    switch (msg.type) {
      case "stream":
        this.handleStream(msg.text);
        break;
      case "finish":
        this.finalizeTurn();
        this.setRunning(false);
        break;
      case "tool_call":
        this.appendToolCall(msg.tool, msg.args, msg.label);
        break;
      case "tool_result":
        this.appendToolResult(msg.tool, msg.result);
        break;
      case "ask_user":
        this.appendAskUser(msg.question);
        break;
      case "permission_request":
        this.appendPermission(msg.tool, msg.description);
        break;
      case "queued_message":
        this.appendSystemNote(`Queued: "${msg.message}" — will run after the current step.`);
        break;
      case "inject":
        this.appendSystemNote(`Injected into the running task: "${msg.message}"`);
        break;
      case "error":
        this.finalizeTurn();
        this.setRunning(false);
        this.appendError(msg.message);
        break;
      case "context_maintenance":
        this.appendSystemNote(msg.message);
        break;
      case "sessions_list":
        this.sessionsList = (msg as any).sessions;
        this.activeSession = (msg as any).active_session_id;
        this.renderSessionsModal();
        break;
      case "session_switched":
        this.activeSession = msg.session_id;
        saveActiveSession(msg.session_id);
        this.resetTurnState();
        if (msg.history && Array.isArray(msg.history)) {
          this.renderHistory(msg.history);
        } else {
          this.messagesEl.replaceChildren();
          this.renderEmptyState();
        }
        this.setRunning(false);
        break;
      case "session_deleted":
        this.sessionsList = this.sessionsList.filter((s) => s.session_id !== msg.session_id);
        if (document.getElementById("sessions-modal")) this.renderSessionsModal();
        break;
      case "settings_updated":
        this.appendSystemNote(msg.success ? "Settings updated." : `Settings update failed: ${msg.message}`);
        break;
    }
  }

  // --------------------------------------------------------- streaming text

  private handleStream(text: string) {
    this.clearEmptyState();
    let rest = text;

    while (rest.length > 0) {
      if (!this.inThinking) {
        const open = rest.indexOf("<thinking>\n");
        if (open === -1) {
          this.appendContent(rest);
          break;
        }
        if (open > 0) this.appendContent(rest.slice(0, open));
        this.inThinking = true;
        this.currentThinkingEl = this.appendThinkingBlock();
        rest = rest.slice(open + "<thinking>\n".length);
      } else {
        const close = rest.indexOf("</thinking>");
        if (close === -1) {
          this.appendThinking(rest);
          break;
        }
        if (close > 0) this.appendThinking(rest.slice(0, close));
        this.endThinking();
        rest = rest.slice(close + "</thinking>".length);
        if (rest.startsWith("\n")) rest = rest.slice(1);
      }
    }

    this.scrollToBottom();
  }

  private appendContent(text: string) {
    if (!text) return;
    if (!this.currentTextEl) {
      this.currentTextEl = this.appendAgentBubble();
      this.currentRawText = "";
    }
    this.currentRawText += text;
    this.scheduleStreamRender();
    if (!this.hasRunningTool()) {
      this.updateDockHeader();
      this.peekDock();
    }
  }

  private scheduleStreamRender() {
    if (this.currentStreamFrame != null) return;
    this.currentStreamFrame = requestAnimationFrame(() => {
      this.currentStreamFrame = null;
      if (!this.currentTextEl) return;
      this.renderMarkdown(this.currentTextEl, this.currentRawText);
      this.scrollToBottom();
    });
  }

  /** Render the buffered markdown immediately and cancel any queued frame. */
  private flushStreamRender() {
    if (this.currentStreamFrame != null) {
      cancelAnimationFrame(this.currentStreamFrame);
      this.currentStreamFrame = null;
    }
    if (this.currentTextEl && this.currentRawText) {
      this.renderMarkdown(this.currentTextEl, this.currentRawText);
    }
  }

  private appendThinking(text: string) {
    if (!text) return;
    if (!this.currentThinkingEl) this.currentThinkingEl = this.appendThinkingBlock();
    this.currentThinkingEl.textContent += text;
    if (this.currentThinkingBoxEl?.dataset.open === "true") {
      const body = this.currentThinkingBoxEl.querySelector(".thinking-body") as HTMLElement | null;
      if (body) body.scrollTop = body.scrollHeight;
    }
  }

  private endThinking() {
    this.inThinking = false;
    this.currentThinkingEl = null;
    if (this.currentThinkingBoxEl) {
      this.currentThinkingBoxEl.dataset.open = "false";
      this.currentThinkingBoxEl.querySelector(".thinking-pulse")?.remove();
    }
  }

  // ---------------------------------------------------------- turn building

  private appendUserTurn(text: string, live: boolean): HTMLElement {
    this.clearEmptyState();
    this.resetStreamState();

    const turnEl = document.createElement("div");
    turnEl.className = "turn-wrapper";

    if (text) {
      const wrap = document.createElement("div");
      wrap.className = "msg user";
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = text;
      wrap.appendChild(bubble);
      turnEl.appendChild(wrap);
    }

    this.messagesEl.appendChild(turnEl);
    this.currentTurnEl = turnEl;

    if (live) this.beginLiveTurn();
    this.scrollToBottom(true);
    return turnEl;
  }

  private beginLiveTurn() {
    if (this.turnTimer !== null) clearInterval(this.turnTimer);
    this.turnRecords = [];
    this.recordSeq = 0;
    this.liveRows.clear();
    this.lastActivity = null;
    this.odometer.set(0, true);
    this.dockListEl.replaceChildren();
    this.closeDock(true);
    this.dockTimeEl.textContent = "0.0s";
    this.dockSubEl.textContent = "";
    this.dockTitleEl.textContent = "Working";
    this.turnStart = Date.now();
    this.turnTimer = window.setInterval(() => {
      this.dockTimeEl.textContent = formatDuration(Date.now() - this.turnStart);
    }, 120);
  }

  private finalizeTurn() {
    if (this.turnTimer !== null) {
      clearInterval(this.turnTimer);
      this.turnTimer = null;
    }

    // Make sure the last markdown frame lands even if finalize beat the rAF.
    this.flushStreamRender();
    if (this.currentThinkingBoxEl) {
      this.currentThinkingBoxEl.dataset.open = "false";
      this.currentThinkingBoxEl.querySelector(".thinking-pulse")?.remove();
    }

    const elapsed = this.turnStart > 0 ? Date.now() - this.turnStart : null;
    for (const record of this.turnRecords) {
      if (record.state === "running") {
        record.state = "ok";
        record.endedAt = Date.now();
      }
    }

    if (this.currentTurnEl && this.turnRecords.length > 0) {
      this.appendSummaryChip(this.currentTurnEl, this.turnRecords, elapsed);
    }

    this.currentTextEl = null;
    this.currentThinkingEl = null;
    this.currentThinkingBoxEl = null;
    this.currentRawText = "";
    this.inThinking = false;
    this.currentTurnEl = null;
    this.turnRecords = [];
    this.liveRows.clear();
    this.lastActivity = null;
    this.turnStart = 0;
    this.closeDock();
  }

  private resetStreamState() {
    this.currentTextEl = null;
    this.currentThinkingEl = null;
    this.currentThinkingBoxEl = null;
    this.currentRawText = "";
    this.inThinking = false;
    if (this.currentStreamFrame != null) {
      cancelAnimationFrame(this.currentStreamFrame);
      this.currentStreamFrame = null;
    }
  }

  private resetTurnState() {
    if (this.turnTimer !== null) {
      clearInterval(this.turnTimer);
      this.turnTimer = null;
    }
    this.resetStreamState();
    this.turnRecords = [];
    this.liveRows.clear();
    this.lastActivity = null;
    this.currentTurnEl = null;
    this.turnStart = 0;
    this.closeDock(true);
  }

  // ------------------------------------------------------------ live tooling

  private appendToolCall(tool: string, rawArgs: unknown, label?: string) {
    this.clearEmptyState();
    // Flush any pending token frame before the step switches to tool output,
    // otherwise the last unrendered markdown chunk is dropped.
    this.flushStreamRender();
    this.currentTextEl = null;
    this.currentRawText = "";

    const args = parseToolArguments(rawArgs);
    const rawOpaque = rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)
      ? (rawArgs as Record<string, unknown>)
      : null;

    // Legacy lifecycle sentinels (e.g. task(finish)) from replayed sessions
    // aren't user-visible work.
    if (describeTool(tool, args ?? rawOpaque).hidden) return;

    // The runtime emits TOOL_CALL before dispatch, and most tools emit their
    // own TOOL_CALL again (sometimes with different argument shapes). The two
    // arrive back-to-back, so collapse same-name events inside a short window.
    // A genuine repeat has a completed result in between.
    const last = this.turnRecords[this.turnRecords.length - 1];
    if (last && last.state === "running" && last.name === tool && Date.now() - last.startedAt < 250) {
      if (label && !last.descriptor.target) {
        last.descriptor.target = label.length > 60 ? `${label.slice(0, 59)}…` : label;
        const row = this.liveRows.get(last.id);
        if (row) this.syncActivityRow(row, last);
      }
      return;
    }

    const record = this.makeRecord(tool, args ?? rawOpaque, undefined, Date.now());
    if (label && !record.descriptor.target) {
      record.descriptor.target = label.length > 60 ? `${label.slice(0, 59)}…` : label;
    }

    this.turnRecords.push(record);
    this.lastActivity = record;

    const row = this.renderActivityRow(record);
    this.liveRows.set(record.id, row);
    this.dockListEl.appendChild(row);

    this.openDock();
    this.odometer.set(this.turnRecords.length);
    this.updateDockHeader();
    this.dockListEl.scrollTop = this.dockListEl.scrollHeight;
    this.scrollToBottom();

    if ((tool === "write_file" || tool === "edit_file" || tool === "find_and_replace_many" || tool === "block") && record.args) {
      const path = record.args.path ?? record.args.file_path;
      if (typeof path === "string") this.fileWrittenHandlers.forEach((h) => h(path));
    }
  }

  private appendToolResult(tool: string, result: unknown) {
    const record = [...this.turnRecords].reverse().find((r) => r.state === "running" && r.name === tool);
    if (!record) return;

    const text = result == null ? "" : typeof result === "string" ? result : String(result);
    enrichToolRecord(record, text);
    this.lastActivity = record;

    const row = this.liveRows.get(record.id);
    if (row) this.syncActivityRow(row, record);
    this.updateDockHeader();
  }

  private makeRecord(
    name: string,
    args: Record<string, unknown> | null,
    result?: string,
    startedAt: number | null = Date.now(),
  ): ToolRecord {
    const record: ToolRecord = {
      id: ++this.recordSeq,
      name,
      args,
      descriptor: describeTool(name, args),
      state: "running",
      result: "",
      startedAt: startedAt ?? 0,
      endedAt: null,
    };
    if (result !== undefined) {
      enrichToolRecord(record, result);
      record.startedAt = 0;
      record.endedAt = null;
    }
    return record;
  }

  private hasRunningTool(): boolean {
    return this.turnRecords.some((r) => r.state === "running");
  }

  // ----------------------------------------------------------- activity dock

  private openDock() {
    if (this.dockClearTimer !== null) {
      clearTimeout(this.dockClearTimer);
      this.dockClearTimer = null;
    }
    this.dockEl.classList.add("open");
    this.dockEl.classList.remove("peek");
    this.dockEl.querySelector(".activity-head")?.setAttribute("aria-expanded", "true");
    this.dockOpen = true;
    this.dockTimeEl.textContent = formatDuration(Date.now() - this.turnStart);
  }

  private peekDock() {
    if (!this.dockOpen || this.turnRecords.length === 0) return;
    this.dockEl.classList.add("peek");
    this.dockEl.querySelector(".activity-head")?.setAttribute("aria-expanded", "false");
  }

  private closeDock(immediate = false) {
    if (this.dockClearTimer !== null) {
      clearTimeout(this.dockClearTimer);
      this.dockClearTimer = null;
    }
    this.dockEl.classList.remove("open", "peek");
    this.dockEl.querySelector(".activity-head")?.setAttribute("aria-expanded", "false");
    this.dockOpen = false;

    const clear = () => {
      if (!this.dockOpen) this.dockListEl.replaceChildren();
      this.dockClearTimer = null;
    };
    if (immediate) clear();
    else this.dockClearTimer = window.setTimeout(clear, 380);
  }

  private toggleDock() {
    if (!this.dockOpen) return;
    const peeked = this.dockEl.classList.toggle("peek");
    this.dockEl.querySelector(".activity-head")?.setAttribute("aria-expanded", String(!peeked));
  }

  private updateDockHeader() {
    const running = this.hasRunningTool();
    this.dockTitleEl.textContent = running ? "Working" : this.currentTextEl ? "Responding" : "Working";
    this.dockSubEl.textContent = this.lastActivity ? activitySummary(this.lastActivity) : "";
    this.dockUnitEl.textContent = this.turnRecords.length === 1 ? "tool" : "tools";
    this.dockEl.dataset.busy = running ? "true" : "false";
  }

  private renderActivityRow(record: ToolRecord): HTMLElement {
    const row = document.createElement("div");
    row.className = "activity-row";
    row.dataset.state = record.state;
    row.dataset.kind = record.descriptor.kind;
    row.innerHTML = `
      <span class="row-icon">${icon(record.descriptor.icon)}</span>
      <span class="row-text">${this.rowTextHtml(record)}</span>
      <span class="row-state"></span>
    `;
    return row;
  }

  private syncActivityRow(row: HTMLElement, record: ToolRecord) {
    row.dataset.state = record.state;
    const textEl = row.querySelector(".row-text");
    if (textEl) textEl.innerHTML = this.rowTextHtml(record);
  }

  private rowTextHtml(record: ToolRecord): string {
    const { verb, target, meta } = record.descriptor;
    let html = `<span class="row-verb">${escapeHtml(verb)}</span>`;
    if (target) html += `<span class="row-target">${escapeHtml(target)}</span>`;
    if (meta) html += `<span class="row-meta">${escapeHtml(meta)}</span>`;
    return html;
  }

  // ---------------------------------------------------------- turn summary

  private appendSummaryChip(turnEl: HTMLElement, records: ToolRecord[], elapsedMs: number | null) {
    const failed = records.some((r) => r.state === "error");
    const count = records.length;
    const title = `${count} tool call${count === 1 ? "" : "s"}`;
    const kinds = kindBreakdown(records);
    const time = elapsedMs != null ? formatDuration(elapsedMs) : "";

    const chip = document.createElement("div");
    chip.className = "turn-summary";
    chip.dataset.open = "false";
    if (failed) chip.dataset.error = "true";

    chip.innerHTML = `
      <button class="summary-head" type="button" aria-expanded="false">
        <span class="summary-icon">${icon(failed ? "x" : "check")}</span>
        <span class="summary-title">${escapeHtml(title)}</span>
        ${kinds ? `<span class="summary-kinds">${escapeHtml(kinds)}</span>` : ""}
        ${time ? `<span class="summary-time">${escapeHtml(time)}</span>` : ""}
        <span class="summary-chev">${icon("chevDown")}</span>
      </button>
      <div class="summary-body">
        <div class="summary-inner">
          <div class="summary-list"></div>
        </div>
      </div>
    `;

    const list = chip.querySelector(".summary-list")!;
    for (const record of records) list.appendChild(this.renderSummaryRow(record));

    const head = chip.querySelector(".summary-head") as HTMLButtonElement;
    head.addEventListener("click", () => {
      const open = chip.dataset.open === "true";
      chip.dataset.open = open ? "false" : "true";
      head.setAttribute("aria-expanded", String(!open));
      if (!open) {
        requestAnimationFrame(() => chip.scrollIntoView({ block: "nearest", behavior: "smooth" }));
      }
    });

    turnEl.appendChild(chip);
    this.scrollToBottom();
  }

  private renderSummaryRow(record: ToolRecord): HTMLElement {
    const row = document.createElement("div");
    row.className = "summary-row";
    row.dataset.state = record.state;
    row.dataset.kind = record.descriptor.kind;

    const duration = record.startedAt > 0 && record.endedAt != null
      ? formatDuration(record.endedAt - record.startedAt)
      : "";

    row.innerHTML = `
      <span class="row-icon">${icon(record.descriptor.icon)}</span>
      <span class="row-text">${this.rowTextHtml(record)}</span>
      ${duration ? `<span class="summary-dur">${escapeHtml(duration)}</span>` : ""}
      <span class="row-state"></span>
    `;
    return row;
  }

  // --------------------------------------------------------- history replay

  private renderHistory(history: unknown[]) {
    this.messagesEl.replaceChildren();
    this.recordSeq = 0;

    const results = new Map<string, string>();
    for (const raw of history) {
      const item = raw as any;
      if (item && item.role === "tool" && item.tool_call_id != null) {
        results.set(String(item.tool_call_id), String(item.content ?? ""));
      }
    }

    let turnEl: HTMLElement | null = null;
    let records: ToolRecord[] = [];

    const flush = () => {
      if (turnEl && records.length > 0) this.appendSummaryChip(turnEl, records, null);
      records = [];
      turnEl = null;
    };

    for (const raw of history) {
      const item = raw as any;
      if (!item || typeof item !== "object") continue;

      if (item.role === "user") {
        const content = String(item.content ?? "");
        if (isInternalHistoryMessage(content)) continue;
        const userText = stripUserInput(content);
        if (!userText.trim()) continue;
        flush();
        turnEl = this.appendUserTurn(userText, false);
        continue;
      }

      if (item.role !== "assistant") continue;
      if (!turnEl) turnEl = this.appendUserTurn("", false);

      const reasoning = String(item.reasoning_content ?? "").trim();
      if (reasoning) this.appendHistoryThinking(turnEl, reasoning);

      const { text, calls } = extractLegacyCalls(String(item.content ?? ""));
      if (text.trim()) this.appendHistoryBubble(turnEl, text.trim());

      const nativeCalls = Array.isArray(item.tool_calls) ? item.tool_calls : [];
      for (const call of nativeCalls) {
        const name = String(call?.function?.name ?? call?.name ?? "").trim();
        if (!name) continue;
        const args = parseToolArguments(call?.function?.arguments ?? call?.arguments);
        if (describeTool(name, args).hidden) continue;
        const result = call?.id != null ? results.get(String(call.id)) : undefined;
        records.push(this.makeRecord(name, args, result, null));
      }
      for (const call of calls) {
        if (describeTool(call.name, call.args).hidden) continue;
        records.push(this.makeRecord(call.name, call.args, "", null));
      }
    }

    flush();

    if (this.messagesEl.children.length === 0) this.renderEmptyState();
    this.scrollToBottom(true);
  }

  private appendHistoryThinking(turnEl: HTMLElement, reasoning: string) {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    const box = document.createElement("div");
    box.className = "thinking-box";
    box.dataset.open = "false";
    box.innerHTML = `
      <div class="thinking-header">
        ${icon("brain")}
        <span>Thought process</span>
        <span class="thinking-chev">${icon("chevDown")}</span>
      </div>
      <div class="thinking-body"></div>
    `;
    const body = box.querySelector(".thinking-body") as HTMLElement;
    body.textContent = reasoning;
    box.querySelector(".thinking-header")!.addEventListener("click", () => {
      box.dataset.open = box.dataset.open === "true" ? "false" : "true";
    });
    wrap.appendChild(box);
    turnEl.appendChild(wrap);
  }

  private appendHistoryBubble(turnEl: HTMLElement, text: string) {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    wrap.appendChild(bubble);
    turnEl.appendChild(wrap);
    this.renderMarkdown(bubble, text);
  }

  // --------------------------------------------------------------- renderers

  private renderMarkdown(el: HTMLElement, raw: string) {
    Promise.resolve(marked.parse(raw)).then((html) => {
      el.innerHTML = DOMPurify.sanitize(html as string);
      el.querySelectorAll("pre").forEach((pre) => {
        const codeEl = pre.querySelector("code");
        let lang = "text";
        if (codeEl) {
          const match = codeEl.className.match(/language-(\w+)/);
          if (match) lang = match[1];
        }
        const header = document.createElement("div");
        header.className = "code-header";
        header.innerHTML = `<span>${escapeHtml(lang)}</span><button class="icon-btn copy-btn" title="Copy code">${icon("files")}</button>`;
        header.querySelector(".copy-btn")!.addEventListener("click", () => {
          navigator.clipboard.writeText(codeEl?.textContent || "");
          const btn = header.querySelector(".copy-btn")!;
          btn.innerHTML = icon("check");
          setTimeout(() => (btn.innerHTML = icon("files")), 2000);
        });
        pre.insertBefore(header, pre.firstChild);
      });
    });
  }

  private renderEmptyState() {
    if (this.messagesEl.children.length > 0) return;
    const el = document.createElement("div");
    el.className = "agent-empty";
    el.innerHTML = `${icon("bot")}<div>Ask the agent to explore, edit, or run something in this workspace.</div>`;
    this.messagesEl.appendChild(el);
  }

  private clearEmptyState() {
    this.messagesEl.querySelector(".agent-empty")?.remove();
  }

  /** Appends a fresh agent bubble, returns the element that receives markdown. */
  private appendAgentBubble(): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    const inner = document.createElement("div");
    inner.className = "bubble";
    wrap.appendChild(inner);
    (this.currentTurnEl ?? this.messagesEl).appendChild(wrap);
    return inner;
  }

  /** Collapsible thinking box: open + pulsing while streaming, collapses at the end. */
  private appendThinkingBlock(): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    const box = document.createElement("div");
    box.className = "thinking-box";
    box.dataset.open = "true";
    box.innerHTML = `
      <div class="thinking-header">
        ${icon("brain")}
        <span>Thinking</span>
        <span class="thinking-pulse"></span>
        <span class="thinking-chev">${icon("chevDown")}</span>
      </div>
      <div class="thinking-body"></div>
    `;
    box.querySelector(".thinking-header")!.addEventListener("click", () => {
      box.dataset.open = box.dataset.open === "true" ? "false" : "true";
    });
    wrap.appendChild(box);
    (this.currentTurnEl ?? this.messagesEl).appendChild(wrap);
    this.currentThinkingBoxEl = box;
    return box.querySelector(".thinking-body")!;
  }

  // ------------------------------------------------------------- interjections

  private appendAskUser(question: string) {
    this.clearEmptyState();
    this.currentTextEl = null;
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    const card = document.createElement("div");
    card.className = "card ask-user";
    card.innerHTML = `
      <div class="row-label">${escapeHtml(question)}</div>
      <input type="text" placeholder="Type your answer…" />
      <div class="actions"><button class="btn primary">Reply</button></div>
    `;
    const input = card.querySelector("input")! as HTMLInputElement;
    const submit = () => {
      const value = input.value.trim();
      if (!value) return;
      agentSocket.send({ type: "event", event_type: "ask_user_response", value });
      card.innerHTML = `<div class="row-label">${escapeHtml(question)}</div><div style="color:var(--fg-1)">You answered: "${escapeHtml(value)}"</div>`;
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    card.querySelector("button")!.addEventListener("click", submit);
    wrap.appendChild(card);
    (this.currentTurnEl ?? this.messagesEl).appendChild(wrap);
    setTimeout(() => input.focus());
    this.scrollToBottom(true);
  }

  /**
   * Permission gate: expands a bar above the composer. The textarea stays
   * live throughout — approve/reject via Esc / Alt+Enter, the buttons, or by
   * typing what you actually want (which rejects this call, then sends).
   */
  private appendPermission(tool: string, description: string) {
    this.clearEmptyState();
    this.currentTextEl = null;
    this.pendingPermission = { tool, description };

    this.permissionBarEl.innerHTML = `
      <div class="perm-title">${icon("zap")} <span><b>${escapeHtml(tool)}</b> wants permission to run</span></div>
      <div class="perm-desc">${escapeHtml(description)}</div>
      <div class="perm-actions">
        <button class="btn minimal deny-btn">Reject <span style="opacity:.6">(Esc)</span></button>
        <button class="btn primary allow-btn">Allow <span style="opacity:.8">(Alt+Enter)</span></button>
      </div>
      <div class="perm-hint">Or type an instruction and send — it'll reject this call and run that instead.</div>
    `;
    // Force reflow before adding the class so the max-height transition plays.
    void this.permissionBarEl.offsetHeight;
    this.permissionBarEl.classList.add("open");

    this.permissionBarEl.querySelector(".allow-btn")!.addEventListener("click", () => this.resolvePermission(true));
    this.permissionBarEl.querySelector(".deny-btn")!.addEventListener("click", () => this.resolvePermission(false));

    this.permissionKeyHandler = (e: KeyboardEvent) => {
      if (!this.pendingPermission) return;
      if (e.key === "Escape") {
        e.preventDefault();
        this.resolvePermission(false);
      } else if (e.altKey && e.key === "Enter") {
        e.preventDefault();
        this.resolvePermission(true);
      }
    };
    window.addEventListener("keydown", this.permissionKeyHandler);
    this.composer.focus();
  }

  private resolvePermission(approve: boolean) {
    if (!this.pendingPermission) return;
    agentSocket.send({ type: "event", event_type: "permission_response", value: approve });
    this.pendingPermission = null;

    if (this.permissionKeyHandler) {
      window.removeEventListener("keydown", this.permissionKeyHandler);
      this.permissionKeyHandler = null;
    }
    if (this.permissionBarEl) {
      this.permissionBarEl.classList.remove("open");
      const bar = this.permissionBarEl;
      setTimeout(() => { if (!this.pendingPermission) bar.innerHTML = ""; }, 280);
    }
    this.composer.focus();
  }

  private appendSystemNote(text: string) {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    wrap.innerHTML = `<div class="card" style="color:var(--fg-1);font-size:12px">${escapeHtml(text)}</div>`;
    (this.currentTurnEl ?? this.messagesEl).appendChild(wrap);
    this.scrollToBottom();
  }

  private appendError(text: string) {
    const wrap = document.createElement("div");
    wrap.className = "msg agent";
    wrap.innerHTML = `<div class="card error">${escapeHtml(text)}</div>`;
    (this.currentTurnEl ?? this.messagesEl).appendChild(wrap);
    this.scrollToBottom(true);
  }

  private scrollToBottom(force = false) {
    if (!force && !this.stickToBottom) return;
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }
}

// ---------------------------------------------------------------- utilities

const INTERNAL_PREFIXES = [
  "[EXECUTION RESULT]",
  "[SYSTEM]",
  "[TOOL_RESULT]",
  "[ENVIRONMENT CHANGE]",
  "[ARCHIVED TASK",
  "[GLOBAL SUMMARY]",
  "[INTERNAL CONTEXT MAINTENANCE]",
  "[Sub-Agent",
];

// Internal terminal notifications are injected into the model context but are
// not user-authored turns; they must never be replayed as chat history.
const ENV_NOTIFICATION_MARKER = "[Environment Detection Notification]";

function isInternalHistoryMessage(content: string): boolean {
  const trimmed = content.trimStart();
  if (trimmed.includes(ENV_NOTIFICATION_MARKER)) return true;
  return INTERNAL_PREFIXES.some((prefix) => trimmed.startsWith(prefix));
}

function stripUserInput(content: string): string {
  const marker = "[USER INPUT]";
  const idx = content.indexOf(marker);
  if (idx !== -1) return content.slice(idx + marker.length).trim();
  return content.trim();
}

/** Extract legacy ```codepilot tool lines from pre-native-calling sessions. */
function extractLegacyCalls(content: string): {
  text: string;
  calls: Array<{ name: string; args: Record<string, unknown> | null }>;
} {
  let text = content.replace(
    /```[a-zA-Z0-9_.-]+\s+filename=[^\n]+\n<<<<<<< CONTENT[\s\S]*?>>>>>>> CONTENT\n```/g,
    "",
  );

  const calls: Array<{ name: string; args: Record<string, unknown> | null }> = [];
  text = text.replace(/```codepilot\n([\s\S]*?)\n```/g, (_match: string, code: string) => {
    for (const line of code.split("\n")) {
      const trimmed = line.trim();
      const match = trimmed.match(/^([a-zA-Z_][\w.]*)\(/);
      if (match) calls.push({ name: match[1], args: parseLegacyArguments(trimmed) });
    }
    return "";
  });

  return { text, calls };
}

function escapeHtml(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
