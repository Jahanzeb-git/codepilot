import monaco from "../../core/monacoSetup";
import { controlSocket } from "../../core/controlSocket";
import { editorSettings } from "../../state/store";
import { icon } from "../icons";

interface OpenTab {
  path: string;
  model: monaco.editor.ITextModel;
  viewState: monaco.editor.ICodeEditorViewState | null;
  dirty: boolean;
  savedVersionId: number;
}

const LANG_MAP: Record<string, string> = {
  ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
  json: "json", md: "markdown", py: "python", rs: "rust", go: "go",
  html: "html", css: "css", scss: "scss", yaml: "yaml", yml: "yaml",
  toml: "toml", sh: "shell", bash: "shell", sql: "sql", c: "c", h: "c",
  cpp: "cpp", hpp: "cpp", java: "java", rb: "ruby", php: "php",
  dockerfile: "dockerfile", txt: "plaintext", xml: "xml",
};

function langFor(path: string): string {
  const name = path.split("/").pop() || "";
  if (name.toLowerCase() === "dockerfile") return "dockerfile";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return LANG_MAP[ext] || "plaintext";
}

export class EditorManager {
  private editor: monaco.editor.IStandaloneCodeEditor | null = null;
  private tabs = new Map<string, OpenTab>();
  private order: string[] = [];
  private activePath: string | null = null;

  private host: HTMLElement;
  private tabbar: HTMLElement;
  private editorHost: HTMLElement;
  private emptyState: HTMLElement;
  private statusPos: HTMLElement;
  private statusLang: HTMLElement;

  constructor(container: HTMLElement) {
    this.host = document.createElement("div");
    this.host.className = "editor-area";
    this.host.innerHTML = `
      <div class="tabbar"></div>
      <div class="editor-host" style="display:none;flex:1"></div>
      <div class="editor-empty">
        ${icon("files")}
        <div>Select a file from the explorer to start editing</div>
      </div>
      <div class="statusbar">
        <div class="seg"><span class="item" data-status="lang">—</span></div>
        <div class="seg"><span class="item" data-status="pos">—</span></div>
      </div>
    `;
    container.appendChild(this.host);
    this.tabbar = this.host.querySelector(".tabbar")!;
    this.editorHost = this.host.querySelector(".editor-host")!;
    this.emptyState = this.host.querySelector(".editor-empty")!;
    this.statusLang = this.host.querySelector('[data-status="lang"]')!;
    this.statusPos = this.host.querySelector('[data-status="pos"]')!;

    monaco.editor.setTheme(editorSettings.get().theme === "dark" ? "vs-dark" : "vs");
    editorSettings.subscribe((s) => this.applySettings(s));

    // Save shortcut works globally while the editor host has focus.
    window.addEventListener("keydown", (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key.toLowerCase() === "s") {
        e.preventDefault();
        this.saveActive();
      }
    });
  }

  async openFile(path: string) {
    const existing = this.tabs.get(path);
    if (existing) {
      this.activate(path);
      return;
    }
    let content: string;
    try {
      content = await controlSocket.readFile(path);
    } catch (e) {
      console.error("failed to read file", path, e);
      throw e instanceof Error ? e : new Error(String(e));
    }
    const uri = monaco.Uri.parse(`file://${path}`);
    const model = monaco.editor.getModel(uri) ?? monaco.editor.createModel(content, langFor(path), uri);
    const tab: OpenTab = { path, model, viewState: null, dirty: false, savedVersionId: model.getAlternativeVersionId() };
    model.onDidChangeContent(() => {
      const t = this.tabs.get(path);
      if (!t) return;
      t.dirty = t.model.getAlternativeVersionId() !== t.savedVersionId;
      this.renderTabs();
    });
    this.tabs.set(path, tab);
    this.order.push(path);
    this.activate(path);
  }

  /** Insert an element (the terminal dock) between the editor surface and the status bar. */
  mountDock(el: HTMLElement) {
    this.host.insertBefore(el, this.host.querySelector(".statusbar"));
  }

  private ensureEditor() {
    if (this.editor) return;
    this.editor = monaco.editor.create(this.editorHost, {
      theme: editorSettings.get().theme === "dark" ? "vs-dark" : "vs",
      fontSize: editorSettings.get().fontSize,
      tabSize: editorSettings.get().tabSize,
      wordWrap: editorSettings.get().wordWrap ? "on" : "off",
      minimap: { enabled: editorSettings.get().minimap },
      automaticLayout: true,
      smoothScrolling: true,
      cursorBlinking: "smooth",
      scrollBeyondLastLine: false,
      fontLigatures: true,
      renderLineHighlight: "gutter",
    });
    this.editor.onDidChangeCursorPosition((e) => {
      this.statusPos.textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`;
    });
  }

  private activate(path: string) {
    const tab = this.tabs.get(path);
    if (!tab) return;

    if (this.activePath) {
      const prev = this.tabs.get(this.activePath);
      if (prev && this.editor) prev.viewState = this.editor.saveViewState();
    }

    this.ensureEditor();
    this.emptyState.style.display = "none";
    this.editorHost.style.display = "block";
    this.editor!.setModel(tab.model);
    if (tab.viewState) this.editor!.restoreViewState(tab.viewState);
    this.editor!.focus();

    this.activePath = path;
    this.statusLang.textContent = langFor(path);
    const pos = this.editor!.getPosition();
    this.statusPos.textContent = pos ? `Ln ${pos.lineNumber}, Col ${pos.column}` : "—";
    this.renderTabs();
  }

  private async closeTab(path: string) {
    const tab = this.tabs.get(path);
    if (!tab) return;
    if (tab.dirty && !confirm(`"${path.split("/").pop()}" has unsaved changes. Close anyway?`)) return;

    tab.model.dispose();
    this.tabs.delete(path);
    this.order = this.order.filter((p) => p !== path);

    if (this.activePath === path) {
      this.activePath = null;
      const next = this.order[this.order.length - 1];
      if (next) this.activate(next);
      else {
        this.editorHost.style.display = "none";
        this.emptyState.style.display = "flex";
        this.editor?.setModel(null);
        this.statusLang.textContent = "—";
        this.statusPos.textContent = "—";
      }
    }
    this.renderTabs();
  }

  async saveActive() {
    if (!this.activePath) return;
    const tab = this.tabs.get(this.activePath);
    if (!tab || !tab.dirty) return;
    try {
      await controlSocket.writeFile(tab.path, tab.model.getValue());
      tab.savedVersionId = tab.model.getAlternativeVersionId();
      tab.dirty = false;
      this.renderTabs();
    } catch (e) {
      console.error("save failed", e);
    }
  }

  /** Reload a file's content from disk if it changed externally and the tab has no unsaved local edits. */
  async reloadFromDiskIfClean(path: string) {
    const tab = this.tabs.get(path);
    if (!tab || tab.dirty) return;
    try {
      const content = await controlSocket.readFile(path);
      if (content !== tab.model.getValue()) {
        const fullRange = tab.model.getFullModelRange();
        tab.model.pushEditOperations(
          [],
          [{ range: fullRange, text: content }],
          () => null
        );
        tab.savedVersionId = tab.model.getAlternativeVersionId();
      }
    } catch {
      /* file may have been deleted; leave tab as-is */
    }
  }

  hasOpenTab(path: string): boolean {
    return this.tabs.has(path);
  }

  private applySettings(s: ReturnType<typeof editorSettings.get>) {
    monaco.editor.setTheme(s.theme === "dark" ? "vs-dark" : "vs");
    this.editor?.updateOptions({
      fontSize: s.fontSize,
      tabSize: s.tabSize,
      wordWrap: s.wordWrap ? "on" : "off",
      minimap: { enabled: s.minimap },
    });
  }

  private renderTabs() {
    this.tabbar.innerHTML = "";
    for (const path of this.order) {
      const tab = this.tabs.get(path)!;
      const name = path.split("/").pop() || path;
      const el = document.createElement("div");
      el.className = "tab" + (path === this.activePath ? " active" : "") + (tab.dirty ? " dirty" : "");
      el.title = path;
      el.innerHTML = `
        <span class="tab-name">${escapeHtml(name)}</span>
        <span class="dot"></span>
        <span class="tab-close">${icon("close")}</span>
      `;
      el.addEventListener("click", (e) => {
        if ((e.target as HTMLElement).closest(".tab-close")) return;
        this.activate(path);
      });
      el.querySelector(".tab-close")!.addEventListener("click", (e) => {
        e.stopPropagation();
        this.closeTab(path);
      });
      this.tabbar.appendChild(el);
    }
  }
}

function escapeHtml(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
