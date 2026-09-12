import type { AgentInbound, AgentOutbound } from "./types";

type MsgHandler = (msg: AgentInbound) => void;
type ConnHandler = (connected: boolean) => void;

/**
 * /ws/agent carries raw NDJSON bytes straight from agent_server.py's Unix
 * socket (see runner/server/src/agent.rs::run_agent_bridge — it fans out
 * `Message::Binary` chunks verbatim, which may not be line-aligned with a
 * single browser message). We buffer and split on '\n' defensively.
 */
export class AgentSocket {
  private ws: WebSocket | null = null;
  private buf = "";
  private handlers = new Set<MsgHandler>();
  private connHandlers = new Set<ConnHandler>();
  private backoff = 500;

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${location.host}/ws/agent`);

    this.ws.onopen = () => {
      this.backoff = 500;
      this.connHandlers.forEach((h) => h(true));
    };

    this.ws.onmessage = async (ev) => {
      let text: string;
      if (typeof ev.data === "string") text = ev.data;
      else if (ev.data instanceof Blob) text = await ev.data.text();
      else text = new TextDecoder().decode(ev.data as ArrayBuffer);

      this.buf += text;
      let idx: number;
      while ((idx = this.buf.indexOf("\n")) !== -1) {
        const line = this.buf.slice(0, idx);
        this.buf = this.buf.slice(idx + 1);
        if (!line.trim()) continue;
        try {
          this.handlers.forEach((h) => h(JSON.parse(line)));
        } catch {
          /* ignore malformed line */
        }
      }
    };

    this.ws.onclose = () => {
      this.connHandlers.forEach((h) => h(false));
      setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 1.6, 8000);
    };

    this.ws.onerror = () => this.ws?.close();
  }

  onMessage(h: MsgHandler) {
    this.handlers.add(h);
    return () => this.handlers.delete(h);
  }
  onConnectionChange(h: ConnHandler) {
    this.connHandlers.add(h);
    return () => this.connHandlers.delete(h);
  }

  send(msg: AgentOutbound) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }
}

export const agentSocket = new AgentSocket();
