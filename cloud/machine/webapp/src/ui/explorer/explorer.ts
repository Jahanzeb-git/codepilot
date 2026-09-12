import { controlSocket } from "../../core/controlSocket";
import type { FsEntry } from "../../core/types";
import { icon } from "../icons";

const WORKSPACE_ROOT = "/workspace";

interface TreeNode {
  entry: FsEntry;
  expanded: boolean;
  children: TreeNode[] | null; // null = not loaded yet
  loading: boolean;
}

type OpenFileHandler = (path: string) => void;

export class Explorer {
  private root: TreeNode = {
    entry: { name: "workspace", path: WORKSPACE_ROOT, is_directory: true, size: 0, modified: 0 },
    expanded: true,
    children: null,
    loading: false,
  };
  private el: HTMLElement;
  private scrollEl: HTMLElement;
  private selectedPath: string | null = null;
  private openHandlers = new Set<OpenFileHandler>();
  private renamingPath: string | null = null;
  /** When true, the rename input is for a brand-new entry that hasn't been created on disk yet. */
  private isNewEntry = false;
  /** Whether the new entry being created is a directory. */
  private newEntryIsDir = false;

  constructor(container: HTMLElement) {
    this.el = document.createElement("div");
    this.el.className = "panel";
    this.el.innerHTML = `
      <div class="panel-header">
        <span>Explorer</span>
        <div class="actions">
          <button class="icon-btn" data-act="new-file" title="New File">${icon("newFile")}</button>
          <button class="icon-btn" data-act="new-folder" title="New Folder">${icon("newFolder")}</button>
          <button class="icon-btn" data-act="refresh" title="Refresh">${icon("refresh")}</button>
          <button class="icon-btn" data-act="collapse" title="Collapse All">${icon("collapseAll")}</button>
        </div>
      </div>
      <div class="explorer-scroll"></div>
    `;
    container.appendChild(this.el);
    this.scrollEl = this.el.querySelector(".explorer-scroll")!;

    this.el.querySelector('[data-act="refresh"]')!.addEventListener("click", () => this.reloadNode(this.root, true));
    this.el.querySelector('[data-act="collapse"]')!.addEventListener("click", () => this.collapseAll());
    this.el.querySelector('[data-act="new-file"]')!.addEventListener("click", () => this.createEntryAtRoot(false));
    this.el.querySelector('[data-act="new-folder"]')!.addEventListener("click", () => this.createEntryAtRoot(true));

    document.addEventListener("click", () => this.closeCtxMenu());

    controlSocket.onFsEvent((kind, path) => this.handleFsEvent(kind, path));
    controlSocket.onConnectionChange((connected) => {
      if (connected) this.reloadNode(this.root, true);
    });
  }

  onOpenFile(h: OpenFileHandler) {
    this.openHandlers.add(h);
  }

  async init() {
    await this.reloadNode(this.root, true);
  }

  // ---------------------------------------------------------------- data

  private findNode(path: string, node: TreeNode = this.root): TreeNode | null {
    if (node.entry.path === path) return node;
    if (!node.children) return null;
    for (const c of node.children) {
      const found = this.findNode(path, c);
      if (found) return found;
    }
    return null;
  }

  private parentPath(path: string): string {
    const idx = path.lastIndexOf("/");
    return idx <= 0 ? WORKSPACE_ROOT : path.slice(0, idx);
  }

  private async reloadNode(node: TreeNode, render: boolean) {
    node.loading = true;
    if (render) this.render();
    try {
      const entries = await controlSocket.listDir(node.entry.path);
      const prevChildren = node.children;
      node.children = entries.map((entry) => {
        const prev = prevChildren?.find((c) => c.entry.path === entry.path);
        return prev ? { ...prev, entry } : { entry, expanded: false, children: null, loading: false };
      });
    } catch (e) {
      node.children = [];
    }
    node.loading = false;
    if (render) this.render();
  }

  private handleFsEvent(kind: "created" | "deleted" | "changed", path: string) {
    if (!path.startsWith(WORKSPACE_ROOT)) return;
    const parent = this.findNode(this.parentPath(path));
    if (!parent || parent.children === null) return; // parent not loaded/expanded; nothing to refresh

    if (kind === "deleted") {
      parent.children = parent.children.filter((c) => c.entry.path !== path);
      this.render();
      return;
    }
    if (kind === "created") {
      // Re-list just this directory to pick up correct metadata (dir vs file).
      this.reloadNode(parent, true);
      return;
    }
    // "changed": only matters for editor content reload, tree shape unaffected.
  }

  // -------------------------------------------------------------- actions

  private async toggle(node: TreeNode) {
    if (!node.entry.is_directory) return;
    node.expanded = !node.expanded;
    if (node.expanded && node.children === null) {
      await this.reloadNode(node, false);
    }
    this.render();
  }

  private select(node: TreeNode) {
    this.selectedPath = node.entry.path;
    if (!node.entry.is_directory) {
      this.openHandlers.forEach((h) => h(node.entry.path));
    } else {
      this.toggle(node);
    }
    this.render();
  }

  private collapseAll() {
    const walk = (n: TreeNode) => {
      if (n !== this.root) n.expanded = false;
      n.children?.forEach(walk);
    };
    walk(this.root);
    this.render();
  }

  private async createEntryAtRoot(isDir: boolean) {
    this.root.expanded = true;
    await this.promptNewEntry(this.root, isDir);
  }

  /**
   * Shows an inline input for the user to type a name BEFORE creating the
   * file/folder. This is the VS Code-style UX — no placeholder files are
   * created on disk until the user confirms a name.
   */
  private async promptNewEntry(parent: TreeNode, isDir: boolean) {
    if (!parent.entry.is_directory) return;
    parent.expanded = true;
    if (parent.children === null) await this.reloadNode(parent, false);

    // Create a transient placeholder node that will show an input field
    const placeholderName = "";
    const path = `${parent.entry.path}/__new__`;
    const placeholder: TreeNode = {
      entry: { name: placeholderName, path, is_directory: isDir, size: 0, modified: Date.now() / 1000 },
      expanded: false,
      children: isDir ? [] : null,
      loading: false,
    };
    parent.children = [...(parent.children || []), placeholder];
    this.renamingPath = path;
    this.isNewEntry = true;
    this.newEntryIsDir = isDir;
    this.render();
  }

  private async commitNewEntry(parent: TreeNode, name: string, isDir: boolean) {
    this.renamingPath = null;
    this.isNewEntry = false;
    name = name.trim();

    // Remove the placeholder node
    if (parent.children) {
      parent.children = parent.children.filter((c) => c.entry.path !== `${parent.entry.path}/__new__`);
    }

    if (!name) {
      this.render();
      return;
    }

    const path = `${parent.entry.path}/${name}`;
    try {
      if (isDir) await controlSocket.createDir(path);
      else await controlSocket.createFile(path);
    } catch (e) {
      console.error("create failed", e);
    }
    await this.reloadNode(parent, true);
  }

  private async createEntry(parent: TreeNode, isDir: boolean) {
    await this.promptNewEntry(parent, isDir);
  }

  private startRename(node: TreeNode) {
    this.renamingPath = node.entry.path;
    this.isNewEntry = false;
    this.render();
  }

  private async commitRename(node: TreeNode, newName: string) {
    this.renamingPath = null;
    this.isNewEntry = false;
    newName = newName.trim();
    if (!newName || newName === node.entry.name) {
      this.render();
      return;
    }
    const parentDir = this.parentPath(node.entry.path);
    const newPath = `${parentDir}/${newName}`;
    try {
      if (node.entry.is_directory) {
        await this.moveDirectory(node.entry.path, newPath);
      } else {
        const content = await controlSocket.readFile(node.entry.path);
        await controlSocket.createFile(newPath);
        await controlSocket.writeFile(newPath, content);
        await controlSocket.deleteFile(node.entry.path);
      }
    } catch (e) {
      console.error("rename failed", e);
    }
    const parent = this.findNode(parentDir);
    if (parent) await this.reloadNode(parent, true);
    else this.render();
  }

  /** No `rename`/`move` op exists on the wire protocol — recreate the tree
   *  under the new path (read + create + write for every file) then remove
   *  the original. Fine for the modest directory sizes a workspace explorer
   *  actually renames; not attempted on multi-GB trees. */
  private async moveDirectory(oldPath: string, newPath: string) {
    await controlSocket.createDir(newPath);
    const entries = await controlSocket.listDir(oldPath);
    for (const e of entries) {
      const rel = e.name;
      if (e.is_directory) {
        await this.moveDirectory(`${oldPath}/${rel}`, `${newPath}/${rel}`);
      } else {
        const content = await controlSocket.readFile(`${oldPath}/${rel}`);
        await controlSocket.createFile(`${newPath}/${rel}`);
        await controlSocket.writeFile(`${newPath}/${rel}`, content);
      }
    }
    await controlSocket.deleteDir(oldPath);
  }

  private async deleteEntry(node: TreeNode) {
    const label = node.entry.is_directory ? "folder" : "file";
    if (!confirm(`Delete ${label} "${node.entry.name}"? This cannot be undone.`)) return;
    try {
      if (node.entry.is_directory) await controlSocket.deleteDir(node.entry.path);
      else await controlSocket.deleteFile(node.entry.path);
    } catch (e) {
      console.error("delete failed", e);
    }
    const parent = this.findNode(this.parentPath(node.entry.path));
    if (parent) await this.reloadNode(parent, true);
  }

  // ---------------------------------------------------------------- menu

  private closeCtxMenu() {
    document.querySelector(".ctx-menu")?.remove();
  }

  private openCtxMenu(ev: MouseEvent, node: TreeNode) {
    ev.preventDefault();
    ev.stopPropagation();
    this.closeCtxMenu();
    const menu = document.createElement("div");
    menu.className = "ctx-menu";
    menu.style.left = `${ev.clientX}px`;
    menu.style.top = `${ev.clientY}px`;

    const items: [string, string, () => void, boolean?][] = node.entry.is_directory
      ? [
          ["newFile", "New File", () => this.createEntry(node, false)],
          ["newFolder", "New Folder", () => this.createEntry(node, true)],
          ["rename", "Rename", () => this.startRename(node)],
          ["trash", "Delete", () => this.deleteEntry(node), true],
        ]
      : [
          ["rename", "Rename", () => this.startRename(node)],
          ["trash", "Delete", () => this.deleteEntry(node), true],
        ];

    for (const [ic, label, fn, danger] of items) {
      const item = document.createElement("div");
      item.className = "ctx-item" + (danger ? " danger" : "");
      item.innerHTML = `${icon(ic as any)}<span>${label}</span>`;
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        this.closeCtxMenu();
        fn();
      });
      menu.appendChild(item);
    }
    document.body.appendChild(menu);
  }

  // -------------------------------------------------------------- render

  private extIcon(name: string): string {
    return icon("fileIcon", "ficon");
  }

  private renderNode(node: TreeNode, depth: number): HTMLElement {
    const row = document.createElement("div");
    row.className = "tree-row" + (this.selectedPath === node.entry.path ? " selected" : "");
    row.style.paddingLeft = `${8 + depth * 16}px`;

    const chev = node.entry.is_directory
      ? `<span class="chev ${node.expanded ? "open" : ""}">${icon("chevRight")}</span>`
      : `<span class="chev leaf">${icon("chevRight")}</span>`;

    const fileIcon = node.entry.is_directory
      ? icon(node.expanded ? "folderOpenIcon" : "folderIcon", "ficon")
      : this.extIcon(node.entry.name);

    if (this.renamingPath === node.entry.path) {
      row.innerHTML = `${chev}${fileIcon}`;
      const input = document.createElement("input");
      input.className = "rename-input";
      input.value = this.isNewEntry ? "" : node.entry.name;
      input.placeholder = this.isNewEntry ? (this.newEntryIsDir ? "folder name" : "filename") : "";
      row.appendChild(input);

      const parentPath = this.parentPath(node.entry.path);
      const parent = this.findNode(parentPath) || this.root;

      setTimeout(() => {
        input.focus();
        if (!this.isNewEntry) {
          const dot = input.value.lastIndexOf(".");
          input.setSelectionRange(0, dot > 0 && !node.entry.is_directory ? dot : input.value.length);
        }
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") input.blur();
        if (e.key === "Escape") {
          if (this.isNewEntry) {
            // Cancel creation — remove placeholder
            if (parent.children) {
              parent.children = parent.children.filter((c) => c.entry.path !== node.entry.path);
            }
            this.renamingPath = null;
            this.isNewEntry = false;
            this.render();
          } else {
            this.renamingPath = null;
            this.isNewEntry = false;
            this.render();
          }
        }
      });
      input.addEventListener("blur", () => {
        if (this.isNewEntry) {
          this.commitNewEntry(parent, input.value, this.newEntryIsDir);
        } else {
          this.commitRename(node, input.value);
        }
      });
      input.addEventListener("click", (e) => e.stopPropagation());
    } else {
      row.innerHTML = `${chev}${fileIcon}<span class="name">${escapeHtml(node.entry.name)}</span>`;
    }

    row.addEventListener("click", () => this.select(node));
    row.addEventListener("contextmenu", (e) => this.openCtxMenu(e, node));

    const wrap = document.createElement("div");
    wrap.className = "tree-group";
    wrap.appendChild(row);

    if (node.entry.is_directory && node.expanded) {
      const childContainer = document.createElement("div");
      childContainer.className = "tree-children";

      if (node.children === null || node.loading) {
        const loading = document.createElement("div");
        loading.className = "tree-row";
        loading.style.paddingLeft = `${8 + (depth + 1) * 16}px`;
        loading.innerHTML = `<span style="color:var(--fg-2);font-size:12px">Loading…</span>`;
        childContainer.appendChild(loading);
      } else if (node.children.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tree-row";
        empty.style.paddingLeft = `${8 + (depth + 1) * 16}px`;
        empty.innerHTML = `<span style="color:var(--fg-2);font-size:12px">Empty</span>`;
        childContainer.appendChild(empty);
      } else {
        for (const child of node.children) {
          childContainer.appendChild(this.renderNode(child, depth + 1));
        }
      }
      wrap.appendChild(childContainer);
    }
    return wrap;
  }

  render() {
    this.scrollEl.innerHTML = "";
    if (this.root.children === null) {
      this.scrollEl.innerHTML = `<div class="explorer-empty">Loading workspace…</div>`;
      return;
    }
    if (this.root.children.length === 0) {
      this.scrollEl.innerHTML = `<div class="explorer-empty">This workspace is empty.<br/>Right-click below, or use the toolbar above, to create a file.</div>`;
    }
    for (const child of this.root.children) {
      this.scrollEl.appendChild(this.renderNode(child, 0));
    }
  }
}

function escapeHtml(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
