import type { ControlRequest, ControlResponse, FsEntry, TerminalCreatedEvent } from "./types";

// Omit<Union, K> collapses to the intersection of members, not the
// per-member omission we want — distribute it manually so each request
// variant keeps its own shape minus `id`.
type DistributiveOmit<T, K extends keyof any> = T extends any ? Omit<T, K> : never;

type FsEventHandler = (type: "created" | "deleted" | "changed", path: string) => void;
type TerminalEventHandler = (ev: TerminalCreatedEvent) => void;
type ConnHandler = (connected: boolean) => void;

let idCounter = 0;
const nextId = () => `c${++idCounter}_${Date.now().toString(36)}`;

/**
 * Thin wrapper over /ws/control. Request/response pairs are correlated by
 * `id`; server-pushed fs_events (no id) fan out to onFsEvent subscribers.
 * Auto-reconnects with backoff so a machine cold-start / suspend-resume
 * cycle on Fly.io doesn't strand the UI.
 */
export class ControlSocket {
  private ws: WebSocket | null = null;
  private pending = new Map<string, { resolve: (v: any) => void; reject: (e: any) => void }>();
  private fsHandlers = new Set<FsEventHandler>();
  private terminalHandlers = new Set<TerminalEventHandler>();
  private connHandlers = new Set<ConnHandler>();
  private backoff = 500;
  private closedByUser = false;

  connect() {
    this.closedByUser = false;
    this.open();
  }

  private open() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${location.host}/ws/control`);

    this.ws.onopen = () => {
      this.backoff = 500;
      this.connHandlers.forEach((h) => h(true));
    };

    this.ws.onmessage = (ev) => {
      let msg: ControlResponse | TerminalCreatedEvent;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if ((msg as TerminalCreatedEvent).event === "terminal_created") {
        this.terminalHandlers.forEach((h) => h(msg as TerminalCreatedEvent));
        return;
      }
      msg = msg as ControlResponse;
      if (msg.type === "file_created" || msg.type === "file_deleted" || msg.type === "file_changed") {
        const kind = msg.type === "file_created" ? "created" : msg.type === "file_deleted" ? "deleted" : "changed";
        this.fsHandlers.forEach((h) => h(kind, msg.path));
        return;
      }
      const id = (msg as any).id;
      const p = id && this.pending.get(id);
      if (p) {
        this.pending.delete(id);
        if ((msg as any).success === false) p.reject(new Error((msg as any).error || "request failed"));
        else p.resolve(msg);
      }
    };

    this.ws.onclose = () => {
      this.connHandlers.forEach((h) => h(false));
      if (!this.closedByUser) {
        setTimeout(() => this.open(), this.backoff);
        this.backoff = Math.min(this.backoff * 1.6, 8000);
      }
    };

    this.ws.onerror = () => this.ws?.close();
  }

  onFsEvent(h: FsEventHandler) {
    this.fsHandlers.add(h);
    return () => this.fsHandlers.delete(h);
  }

  onTerminalEvent(h: TerminalEventHandler) {
    this.terminalHandlers.add(h);
    return () => this.terminalHandlers.delete(h);
  }

  onConnectionChange(h: ConnHandler) {
    this.connHandlers.add(h);
    return () => this.connHandlers.delete(h);
  }

  private send<T>(req: DistributiveOmit<ControlRequest, "id">): Promise<T> {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("not connected"));
        return;
      }
      const id = nextId();
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ ...req, id }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error("timeout"));
        }
      }, 15000);
    });
  }

  listDir(path: string) {
    return this.send<{ data: FsEntry[] }>({ type: "list_dir", path }).then((r) => r.data);
  }
  readFile(path: string) {
    return this.send<{ content: string }>({ type: "read_file", path }).then((r) => r.content);
  }
  writeFile(path: string, content: string) {
    return this.send<{ success: boolean }>({ type: "write_file", path, content });
  }
  createFile(path: string) {
    return this.send<{ success: boolean }>({ type: "create_file", path });
  }
  deleteFile(path: string) {
    return this.send<{ success: boolean }>({ type: "delete_file", path });
  }
  createDir(path: string) {
    return this.send<{ success: boolean }>({ type: "create_dir", path });
  }
  deleteDir(path: string) {
    return this.send<{ success: boolean }>({ type: "delete_dir", path });
  }
  gitClone(url: string) {
    return this.send<{ success: boolean; error?: string }>({ type: "git_clone", url } as any);
  }
}

export const controlSocket = new ControlSocket();
