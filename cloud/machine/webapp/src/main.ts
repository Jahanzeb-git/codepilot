import { controlSocket } from "./core/controlSocket";
import { agentSocket } from "./core/agentSocket";
import { Explorer } from "./ui/explorer/explorer";
import { EditorManager } from "./ui/editor/editor";
import { AgentPanel } from "./ui/agent/agent";
import { SettingsPanel } from "./ui/settings/settings";
import { TerminalDock } from "./ui/terminal/terminalDock";
import { icon } from "./ui/icons";

type Side = "explorer" | "agent";

const PANEL_WIDTH_KEY = "codepilot.sidePanelWidth";

const app = document.getElementById("app")!;

// -------------------------------------------------------------- layout

import { mountIcon } from "./ui/react-icons/mount";
import { 
  FileDescriptionIcon, 
  SparklesIcon, 
  CodeIcon, 
  GearIcon, 
  UserIcon, 
  TerminalIcon,
  UploadIcon
} from "./ui/react-icons/icons";

const sidebar = document.createElement("div");
sidebar.className = "sidebar";
sidebar.innerHTML = `
  <div class="sidebar-top">
    <button class="side-btn" data-side="explorer" title="Explorer"><div id="react-icon-explorer" style="width:20px;height:20px"></div></button>
    <button class="side-btn" data-side="agent" title="Agent"><div id="react-icon-agent" style="width:20px;height:20px"></div></button>
    <button class="side-btn" data-action="upload" title="Import from Folder / GitHub"><div id="react-icon-upload" style="width:20px;height:20px"></div></button>
  </div>
  <div class="sidebar-bottom">
    <button class="side-btn" data-action="toggle-terminal" title="Terminal (Ctrl+\`)"><div id="react-icon-terminal" style="width:20px;height:20px"></div></button>
    <button class="side-btn" data-action="open-file" title="Open File by Path"><div id="react-icon-open-file" style="width:20px;height:20px"></div></button>
    <button class="side-btn" data-action="profile" title="Profile"><div id="react-icon-profile" style="width:20px;height:20px"></div></button>
    <button class="side-btn" data-action="settings" title="Settings"><div id="react-icon-settings" style="width:20px;height:20px"></div></button>
  </div>
`;
app.appendChild(sidebar);

mountIcon(sidebar.querySelector("#react-icon-explorer")!, FileDescriptionIcon);
mountIcon(sidebar.querySelector("#react-icon-agent")!, SparklesIcon);
mountIcon(sidebar.querySelector("#react-icon-upload")!, UploadIcon);
mountIcon(sidebar.querySelector("#react-icon-terminal")!, TerminalIcon);
mountIcon(sidebar.querySelector("#react-icon-open-file")!, CodeIcon);
mountIcon(sidebar.querySelector("#react-icon-profile")!, UserIcon);
mountIcon(sidebar.querySelector("#react-icon-settings")!, GearIcon);

const sidePanelHost = document.createElement("div");
sidePanelHost.style.display = "contents";
app.appendChild(sidePanelHost);

// Drag handle for resizing the side panel
const resizeHandle = document.createElement("div");
resizeHandle.className = "panel-resize-handle";
app.appendChild(resizeHandle);

const editorHost = document.createElement("div");
editorHost.style.display = "contents";
app.appendChild(editorHost);

// ----------------------------------------------------------- components

const explorerContainer = document.createElement("div");
explorerContainer.style.display = "contents";
sidePanelHost.appendChild(explorerContainer);
const explorer = new Explorer(explorerContainer);

const agentContainer = document.createElement("div");
agentContainer.style.display = "contents";
sidePanelHost.appendChild(agentContainer);

const settings = new SettingsPanel(document.body);
new AgentPanel(agentContainer);

const editor = new EditorManager(editorHost);
settings.setOpenFileHandler((path) => editor.openFile(path));

const terminalDock = new TerminalDock();
editor.mountDock(terminalDock.el);

sidebar.querySelector('[data-action="upload"]')!.addEventListener("click", () => renderImportModal());

function renderImportModal() {
  const existing = document.getElementById("import-modal");
  if (existing) existing.remove();

  const overlay = document.createElement("div");
  overlay.id = "import-modal";
  overlay.className = "modal-overlay";
  
  overlay.innerHTML = `
    <div class="modal import-modal">
      <div class="modal-header">
        <h3>Import Project</h3>
        <button class="icon-btn close-btn">${icon("close")}</button>
      </div>
      <div class="import-body">
        <div class="import-section">
          <h4>From your machine</h4>
          <p>Select a folder to upload all files to the workspace.</p>
          <button class="btn primary" id="btn-upload-folder">Select Folder</button>
          <input type="file" id="folder-input" webkitdirectory directory multiple style="display:none;" />
        </div>
        <div class="import-divider">OR</div>
        <div class="import-section">
          <h4>From GitHub</h4>
          <p>Clone a public repository directly into the workspace.</p>
          <div style="display:flex; gap:8px;">
            <input type="text" id="github-url" placeholder="https://github.com/user/repo" style="flex:1;" />
            <button class="btn primary" id="btn-clone-repo">Clone</button>
          </div>
        </div>
        <div id="import-status" style="margin-top:16px; font-size:12px; color:var(--fg-1); display:none;"></div>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  overlay.querySelector(".close-btn")!.addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  const statusEl = overlay.querySelector("#import-status") as HTMLElement;
  const setStatus = (msg: string) => {
    statusEl.style.display = "block";
    statusEl.textContent = msg;
  };

  const folderInput = overlay.querySelector("#folder-input") as HTMLInputElement;
  overlay.querySelector("#btn-upload-folder")!.addEventListener("click", () => {
    folderInput.click();
  });

  folderInput.addEventListener("change", async () => {
    if (!folderInput.files || folderInput.files.length === 0) return;
    setStatus(`Uploading ${folderInput.files.length} files...`);
    const files = Array.from(folderInput.files);
    
    let successCount = 0;
    let failCount = 0;
    for (const file of files) {
      // Ignore hidden directories like .git
      if (file.webkitRelativePath.includes("/.git/")) continue;
      
      try {
        const content = await file.text();
        // The path will be like "project_name/src/main.ts". We might want to upload it directly to workspace root?
        // Let's keep the project_name folder as the root.
        await controlSocket.writeFile(file.webkitRelativePath, content);
        successCount++;
        setStatus(`Uploaded ${successCount}/${files.length}...`);
      } catch (e) {
        failCount++;
      }
    }
    setStatus(`Done. Uploaded ${successCount} files. ${failCount > 0 ? `Failed ${failCount} files (likely binary).` : ''}`);
  });

  overlay.querySelector("#btn-clone-repo")!.addEventListener("click", async () => {
    const url = (overlay.querySelector("#github-url") as HTMLInputElement).value.trim();
    if (!url) return;
    setStatus(`Cloning repository...`);
    try {
      const res = await controlSocket.gitClone(url);
      if (res.success) {
        setStatus("Successfully cloned repository!");
      } else {
        setStatus(`Failed to clone: ${(res as any).error}`);
      }
    } catch (e) {
      setStatus(`Error: ${e}`);
    }
  });
}

// -------------------------------------------------------------- wiring

explorer.onOpenFile((path) => editor.openFile(path));

// When any file is modified externally (e.g. by agent, script, or user in terminal),
// refresh it from disk if it's open in the editor and has no unsaved local changes.
controlSocket.onFsEvent((kind, path) => {
  if (kind === "changed" && editor.hasOpenTab(path)) {
    editor.reloadFromDiskIfClean(path);
  }
});

// -------------------------------------------------------------- sidebar

function showSide(side: Side) {
  sidebar.querySelectorAll("[data-side]").forEach((b) =>
    b.classList.toggle("active", b.getAttribute("data-side") === side)
  );
  explorerContainer.querySelector(".panel")!.toggleAttribute("hidden", side !== "explorer");
  agentContainer.querySelector(".panel")!.toggleAttribute("hidden", side !== "agent");
}
sidebar.querySelectorAll("[data-side]").forEach((btn) =>
  btn.addEventListener("click", () => showSide(btn.getAttribute("data-side") as Side))
);
sidebar.querySelector('[data-action="settings"]')!.addEventListener("click", () => settings.open());
sidebar.querySelector('[data-action="toggle-terminal"]')!.addEventListener("click", () => terminalDock.toggle());
showSide("explorer");

// ---------------------------------------------------------- panel resize

let panelWidth = Number(localStorage.getItem(PANEL_WIDTH_KEY)) || 280;
app.style.gridTemplateColumns = `var(--sidebar-w) ${panelWidth}px 4px 1fr`;

{
  let dragging = false;
  let startX = 0;
  let startW = 0;

  resizeHandle.addEventListener("mousedown", (e: MouseEvent) => {
    dragging = true;
    startX = e.clientX;
    startW = panelWidth;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  });

  window.addEventListener("mousemove", (e: MouseEvent) => {
    if (!dragging) return;
    const delta = e.clientX - startX;
    panelWidth = Math.max(200, Math.min(startW + delta, 600));
    app.style.gridTemplateColumns = `var(--sidebar-w) ${panelWidth}px 4px 1fr`;
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    localStorage.setItem(PANEL_WIDTH_KEY, String(panelWidth));
  });
}

// --------------------------------------------------------- open file modal

sidebar.querySelector('[data-action="open-file"]')!.addEventListener("click", () => openFileModal());

function openFileModal() {
  // Remove existing modal if any
  document.querySelector(".open-file-overlay")?.remove();

  const overlay = document.createElement("div");
  overlay.className = "open-file-overlay";
  overlay.innerHTML = `
    <div class="open-file-modal">
      <div class="open-file-header">
        <span>Open File</span>
        <button class="icon-btn" data-close>${icon("close")}</button>
      </div>
      <div class="open-file-body">
        <div class="open-file-desc">Enter a file path within <code>/workspace</code> to open it in the editor.</div>
        <div class="open-file-input-row">
          <input type="text" placeholder="/workspace/path/to/file.py" autofocus />
          <button class="btn primary">Open</button>
        </div>
        <div class="open-file-error" hidden></div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const input = overlay.querySelector("input")! as HTMLInputElement;
  const errorEl = overlay.querySelector(".open-file-error")! as HTMLElement;
  const openBtn = overlay.querySelector(".btn.primary")! as HTMLButtonElement;

  const close = () => overlay.remove();

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  overlay.querySelector("[data-close]")!.addEventListener("click", close);

  const doOpen = async () => {
    let path = input.value.trim();
    if (!path) return;
    if (!path.startsWith("/")) {
      path = "/workspace/" + path; // default to workspace if relative
    }

    try {
      errorEl.hidden = true;
      await editor.openFile(path);
      close();
    } catch (e) {
      errorEl.textContent = `Failed to open: ${e}`;
      errorEl.hidden = false;
    }
  };

  openBtn.addEventListener("click", doOpen);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doOpen();
    if (e.key === "Escape") close();
  });

  setTimeout(() => input.focus());
}

// Keyboard shortcut: Ctrl+O
window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "o") {
    e.preventDefault();
    openFileModal();
  }
});

// --------------------------------------------------------------- boot

controlSocket.connect();
agentSocket.connect();
explorer.init();
