type DataHandler = (bytes: Uint8Array) => void;
type StateHandler = (state: TerminalState) => void;

export type TerminalState = "connecting" | "open" | "closed";

/**
 * One WebSocket per terminal session, talking to the server's unified
 * /ws/terminal?session={id} endpoint (routes.rs picks agent-bridge vs.
 * private-PTY server-side by checking whether /tmp/codepilot_{id}.sock
 * exists — the client doesn't need to know which one it got).
 *
 * Handshake: first Text message is `{"pid":...,"cols":...,"rows":...}`,
 * everything after is raw VT100 bytes both ways (see terminal.rs).
 *
 * Reconnect: only for agent-bridged sessions. Per the backend's own
 * comment, a runtime re-init swaps the Unix socket's inode, the bridge
 * hits EOF and closes our WS — reconnecting re-runs the server's
 * `tokio::fs::metadata` check against the *current* inode, so simply
 * opening a fresh WebSocket at the same session id is enough to "follow"
 * the swap. Manual user PTYs are NOT reconnected on close — an exited
 * bash is a normal terminal state, not a stale-socket condition, and
 * silently spawning a new shell under the user would be surprising.
 */
export class TerminalSocket {
  private ws: WebSocket | null = null;
  private dataHandlers = new Set<DataHandler>();
  private stateHandlers = new Set<StateHandler>();
  private gotHandshake = false;
  private backoff = 500;
  private closedByUser = false;
  public pid: number | null = null;

  constructor(
    public readonly sessionId: string,
    private readonly reconnect: boolean
  ) {}

  connect() {
    this.closedByUser = false;
    this.open();
  }

  private open() {
    this.setState("connecting");
    this.gotHandshake = false;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${location.host}/ws/terminal?session=${encodeURIComponent(this.sessionId)}`);
    this.ws.binaryType = "arraybuffer";

    this.ws.onopen = () => {
      this.backoff = 500;
    };

    this.ws.onmessage = (ev) => {
      if (!this.gotHandshake) {
        // First frame is always the JSON handshake line, sent as Text.
        if (typeof ev.data === "string") {
          try {
            const hs = JSON.parse(ev.data);
            this.pid = hs.pid ?? null;
          } catch {
            /* ignore */
          }
        }
        this.gotHandshake = true;
        this.setState("open");
        return;
      }
      const bytes =
        ev.data instanceof ArrayBuffer
          ? new Uint8Array(ev.data)
          : typeof ev.data === "string"
          ? new TextEncoder().encode(ev.data)
          : new Uint8Array();
      this.dataHandlers.forEach((h) => h(bytes));
    };

    this.ws.onclose = () => {
      this.setState("closed");
      if (!this.closedByUser && this.reconnect) {
        setTimeout(() => this.open(), this.backoff);
        this.backoff = Math.min(this.backoff * 1.6, 8000);
      }
    };
    this.ws.onerror = () => this.ws?.close();
  }

  onData(h: DataHandler) {
    this.dataHandlers.add(h);
    return () => this.dataHandlers.delete(h);
  }
  onState(h: StateHandler) {
    this.stateHandlers.add(h);
    return () => this.stateHandlers.delete(h);
  }
  private setState(s: TerminalState) {
    this.stateHandlers.forEach((h) => h(s));
  }

  write(data: string) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(new TextEncoder().encode(data));
  }
  resize(cols: number, rows: number) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ cols, rows }));
  }
  /** Manual reconnect for a user PTY the person explicitly wants to restart. */
  restart() {
    this.closedByUser = false;
    this.backoff = 500;
    this.open();
  }
  dispose() {
    this.closedByUser = true;
    this.ws?.close();
  }
}
