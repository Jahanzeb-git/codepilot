import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { FitAddon } from "@xterm/addon-fit";
import { TerminalSocket, type TerminalState } from "../../core/terminalSocket";
import { controlSocket } from "../../core/controlSocket";
import { editorSettings } from "../../state/store";
import { icon } from "../icons";

interface Tab {
  sessionId: string;
  label: string;
  kind: "agent" | "user";
  closable: boolean;
  term: Terminal;
  fit: FitAddon;
  socket: TerminalSocket | null; // null until we're allowed to connect (agent tabs wait for terminal_created)
  container: HTMLElement;
  state: TerminalState | "waiting";
}

const THEME = {
  dark: {
    background: "#1e1e1e",
    foreground: "#cccccc",
    cursor: "#cccccc",
    selectionBackground: "#264f78",
    black: "#1e1e1e", red: "#f14c4c", green: "#89d185", yellow: "#cca700",
    blue: "#3794ff", magenta: "#d670d6", cyan: "#29b8db", white: "#cccccc",
    brightBlack: "#666666", brightRed: "#f14c4c", brightGreen: "#89d185",
    brightYellow: "#cca700", brightBlue: "#3794ff", brightMagenta: "#d670d6",
    brightCyan: "#29b8db", brightWhite: "#e5e5e5",
  },
  light: {
    background: "#ffffff",
    foreground: "#1e1e1e",
    cursor: "#1e1e1e",
    selectionBackground: "#add6ff",
    black: "#1e1e1e", red: "#cd3131", green: "#00863b", yellow: "#9a6700",
    blue: "#0066b8", magenta: "#af00db", cyan: "#0089b3", white: "#5f5f5f",
    brightBlack: "#666666", brightRed: "#cd3131", brightGreen: "#00863b",
    brightYellow: "#9a6700", brightBlue: "#0066b8", brightMagenta: "#af00db",
    brightCyan: "#0089b3", brightWhite: "#1e1e1e",
  },
};

const DOCK_HEIGHT_KEY = "codepilot.terminalDockHeight";
const DOCK_OPEN_KEY = "codepilot.terminalDockOpen";

export class TerminalDock {
  readonly el: HTMLElement;
  private tabbarEl: HTMLElement;
  private bodyEl: HTMLElement;
  private resizeHandle: HTMLElement;
  private tabs = new Map<string, Tab>();
  private order: string[] = [];
  private activeId: string | null = null;
  private open: boolean;
  private height: number;
  private resizeObserver: ResizeObserver;

  constructor() {
    this.open = localStorage.getItem(DOCK_OPEN_KEY) !== "0";
    this.height = Number(localStorage.getItem(DOCK_HEIGHT_KEY)) || 260;

    this.el = document.createElement("div");
    this.el.className = "term-dock";
    this.el.innerHTML = `
      <div class="term-resize-handle"></div>
      <div class="term-main">
        <div class="term-body"></div>
        <div class="term-tabbar"></div>
      </div>
    `;
    this.tabbarEl = this.el.querySelector(".term-tabbar")!;
    this.bodyEl = this.el.querySelector(".term-body")!;
    this.resizeHandle = this.el.querySelector(".term-resize-handle")!;

    this.setupResize();
    this.applyOpenState();

    this.resizeObserver = new ResizeObserver(() => this.fitActive());
    this.resizeObserver.observe(this.bodyEl);

    editorSettings.subscribe(() => this.applyTheme());

    // The permanent "agent" tab — always present, tied to the codepilot
    // runtime's default session. We don't open its socket yet: connecting
    // before the server can confirm /tmp/codepilot_main.sock exists risks
    // routes.rs's existence check racing the runtime's own startup and
    // silently handing us a private bash PTY instead of the real bridge.
    // We wait for the "main" terminal_created replay/broadcast instead.
    this.addTab("main", "agent", "agent", false);

    controlSocket.onTerminalEvent((ev) => this.handleTerminalCreated(ev.session_id));

    window.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "`") {
        e.preventDefault();
        this.toggle();
      }
    });
  }

  // ------------------------------------------------------------- lifecycle

  private handleTerminalCreated(sessionId: string) {
    const label = sessionId === "main" ? "agent" : sessionId;
    const tab = this.tabs.get(sessionId) ?? this.addTab(sessionId, label, "agent", sessionId !== "main");
    if (!tab.socket) this.connectTab(tab);
  }

  newUserTerminal() {
    const id = `local_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    const n = this.order.filter((s) => this.tabs.get(s)?.kind === "user").length + 1;
    const tab = this.addTab(id, `bash ${n}`, "user", true);
    this.connectTab(tab);
    this.activate(id);
    this.setOpenState(true);
  }

  private addTab(sessionId: string, label: string, kind: "agent" | "user", closable: boolean): Tab {
    const container = document.createElement("div");
    container.className = "term-pane";
    container.style.display = "none";
    this.bodyEl.appendChild(container);

    const term = new Terminal({
      fontSize: editorSettings.get().fontSize,
      fontFamily: "'SF Mono', Menlo, Consolas, 'Cascadia Code', monospace",
      cursorBlink: true,
      scrollback: 5000,
      theme: THEME[editorSettings.get().theme],
      allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(container);

    term.onData((data) => this.tabs.get(sessionId)?.socket?.write(data));

    const tab: Tab = { sessionId, label, kind, closable, term, fit, socket: null, container, state: "waiting" };
    this.tabs.set(sessionId, tab);
    this.order.push(sessionId);
    this.printStatus(tab, kind === "agent" ? "Waiting for agent runtime…" : "Starting shell…");
    this.renderTabs();
    if (!this.activeId) this.activate(sessionId);
    return tab;
  }

  private connectTab(tab: Tab) {
    const socket = new TerminalSocket(tab.sessionId, tab.kind === "agent");
    tab.socket = socket;
    let everOpened = false;
    socket.onState((s) => {
      tab.state = s;
      this.renderTabs();
      if (s === "open") {
        if (everOpened && tab.kind === "agent") {
          // The upstream terminal multiplexer disconnects clients whenever a
          // burst of output can't be flushed to them immediately (see
          // codepilot-ai's MuxServer) — reconnecting only replays a short
          // scrollback, not the exact byte we left off at. Make that visible
          // instead of leaving a silent gap that looks like the UI is stuck.
          tab.term.write("\r\n\x1b[90m[reconnected — some output may have been missed]\x1b[0m\r\n");
        }
        everOpened = true;
        setTimeout(() => {
          this.fitActive();
          tab.term.focus();
        }, 50);
      }
      if (s === "closed" && tab.kind === "user") {
        this.printStatus(tab, "\r\n\x1b[90m[process exited — press any key to restart]\x1b[0m");
      }
    });
    socket.onData((bytes) => tab.term.write(bytes));
    socket.connect();

    // One-shot: if a closed user shell gets a keypress, restart it instead
    // of forwarding dead bytes into the void.
    if (tab.kind === "user") {
      const disp = tab.term.onData(() => {
        if (tab.socket && tab.state === "closed") {
          tab.term.reset();
          tab.socket.restart();
        }
      });
      void disp;
    }
  }

  private printStatus(tab: Tab, text: string) {
    tab.term.write(`\x1b[90m${text}\x1b[0m`);
  }

  private closeTab(sessionId: string) {
    const tab = this.tabs.get(sessionId);
    if (!tab || !tab.closable) return;
    tab.socket?.dispose();
    tab.term.dispose();
    tab.container.remove();
    this.tabs.delete(sessionId);
    this.order = this.order.filter((s) => s !== sessionId);
    if (this.activeId === sessionId) {
      this.activeId = null;
      const next = this.order[this.order.length - 1];
      if (next) this.activate(next);
    }
    this.renderTabs();
  }

  private activate(sessionId: string) {
    const tab = this.tabs.get(sessionId);
    if (!tab) return;
    if (this.activeId) {
      const prev = this.tabs.get(this.activeId);
      if (prev) prev.container.style.display = "none";
    }
    tab.container.style.display = "block";
    this.activeId = sessionId;
    this.renderTabs();
    requestAnimationFrame(() => {
      this.fitActive();
      tab.term.focus();
    });
  }

  private fitActive() {
    if (!this.open || !this.activeId) return;
    const tab = this.tabs.get(this.activeId);
    if (!tab) return;
    try {
      tab.fit.fit();
      tab.socket?.resize(tab.term.cols, tab.term.rows);
    } catch {
      /* container not yet laid out */
    }
  }

  private applyTheme() {
    const s = editorSettings.get();
    for (const tab of this.tabs.values()) {
      tab.term.options.theme = THEME[s.theme];
      tab.term.options.fontSize = s.fontSize;
    }
    this.fitActive();
  }

  // ------------------------------------------------------------------ dock

  toggle() {
    this.setOpenState(!this.open);
  }
  private setOpenState(open: boolean) {
    this.open = open;
    localStorage.setItem(DOCK_OPEN_KEY, open ? "1" : "0");
    this.applyOpenState();
    if (open) requestAnimationFrame(() => this.fitActive());
  }
  private applyOpenState() {
    this.el.style.height = this.open ? `${this.height}px` : "0px";
    this.el.style.borderTopWidth = this.open ? "1px" : "0px";
    this.el.style.display = this.open ? "flex" : "none";
    this.resizeHandle.style.pointerEvents = this.open ? "auto" : "none";
  }

  private setupResize() {
    let dragging = false;
    let startY = 0;
    let startH = 0;
    this.resizeHandle.addEventListener("mousedown", (e) => {
      if (!this.open) return;
      dragging = true;
      startY = e.clientY;
      startH = this.height;
      this.resizeHandle.classList.add("active");
      document.body.style.cursor = "row-resize";
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const delta = startY - e.clientY;
      this.height = Math.min(Math.max(startH + delta, 120), window.innerHeight - 160);
      this.el.style.height = `${this.height}px`;
      this.fitActive();
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      this.resizeHandle.classList.remove("active");
      document.body.style.cursor = "";
      localStorage.setItem(DOCK_HEIGHT_KEY, String(this.height));
    });
  }

  private renderTabs() {
    this.tabbarEl.innerHTML = `
      <div class="term-tabbar-header">
        <span>Terminals</span>
        <button class="icon-btn" id="new-term-btn" title="New Terminal (bash)">${icon("plus")}</button>
      </div>
    `;
    this.tabbarEl.querySelector("#new-term-btn")!.addEventListener("click", () => this.newUserTerminal());

    for (const id of this.order) {
      const tab = this.tabs.get(id)!;
      const el = document.createElement("div");
      el.className = "term-sidebar-item" + (id === this.activeId ? " active" : "");

      const stateDot = tab.state === "waiting" ? "" : `<span class="term-state-dot ${tab.state}"></span>`;

      el.innerHTML = `
        <div class="ti-left">
          ${icon(tab.kind === "agent" ? "bot" : "terminal")}
          <span class="ti-label">${escapeHtml(tab.label)}</span>
        </div>
        <div class="ti-right">
          ${stateDot}
          ${tab.closable ? `<div class="term-close-btn" title="Close">${icon("trash")}</div>` : ""}
        </div>
      `;

      el.addEventListener("click", (e) => {
        if ((e.target as HTMLElement).closest(".term-close-btn")) {
          this.closeTab(id);
          return;
        }
        this.activate(id);
      });

      this.tabbarEl.appendChild(el);
    }

    const footer = document.createElement("div");
    footer.className = "term-tabbar-footer";
    const toggleBtn = document.createElement("button");
    toggleBtn.className = "icon-btn";
    toggleBtn.title = "Hide Terminal (Ctrl+`)";
    toggleBtn.innerHTML = icon("chevDown");
    toggleBtn.addEventListener("click", () => this.toggle());
    footer.appendChild(toggleBtn);
    this.tabbarEl.appendChild(footer);
  }
}

function escapeHtml(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
