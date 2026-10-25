import type { IconName } from "../icons";

/**
 * Semantic presentation for tool calls.
 *
 * Raw tool names (`view_file`, `edit_file`, ...) are an implementation
 * detail. Everything the agent panel renders goes through this module so a
 * call reads like an action — "Reading app.ts L1–20" — instead of a function
 * signature. It also normalises the result payload into a short piece of
 * metadata ("replaced 3 → 5 lines", "exit 0") so completed rows stay useful
 * without opening a log.
 */

export type ToolState = "running" | "ok" | "error";

export type ToolKind =
  | "read"
  | "write"
  | "edit"
  | "run"
  | "search"
  | "web"
  | "agent"
  | "control";

export interface ToolDescriptor {
  /** Leading verb: "Reading", "Editing", "Running". */
  verb: string;
  /** Primary noun: a file, a command, a query. May be empty. */
  target: string;
  /** Compact trailing detail: "L1–20", "exit 0". May be empty. */
  meta: string;
  /** Icon key from `icons.ts`. */
  icon: IconName;
  kind: ToolKind;
  /**
   * Internal lifecycle signals (e.g. `task`) are not work the user asked for.
   * They stay out of the activity dock and the tool-call count.
   */
  hidden?: boolean;
}

export interface ToolRecord {
  id: number;
  name: string;
  args: Record<string, unknown> | null;
  descriptor: ToolDescriptor;
  state: ToolState;
  result: string;
  startedAt: number;
  endedAt: number | null;
}

// ------------------------------------------------------------------ helpers

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function oneLine(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function truncate(value: string, max: number): string {
  const flat = oneLine(value);
  if (flat.length <= max) return flat;
  return flat.slice(0, Math.max(1, max - 1)).trimEnd() + "…";
}

/** Last path segment — enough to identify a file, short enough for a row. */
export function basename(path: string): string {
  const clean = path.replace(/[\\/]+$/, "");
  const parts = clean.split(/[\\/]/);
  return parts[parts.length - 1] || clean;
}

function rangeLabel(args: Record<string, unknown> | null): string {
  if (!args) return "";
  const start = typeof args.start_line === "number" ? args.start_line : 1;
  const end = typeof args.end_line === "number" ? args.end_line : null;
  if (end != null) return `L${start}–${end}`;
  return `L${start}…`;
}

// --------------------------------------------------------------- descriptors

export function describeTool(name: string, rawArgs: unknown): ToolDescriptor {
  const args = asRecord(rawArgs);
  const path = text(args?.path ?? args?.file_path);
  const file = path ? basename(path) : "";

  switch (name) {
    case "view_file":
      return {
        verb: "Reading",
        target: file || "file",
        meta: rangeLabel(args),
        icon: "fileText",
        kind: "read",
      };

    case "write_file":
      return {
        verb: "Writing",
        target: file || "file",
        meta: "",
        icon: "openFile",
        kind: "write",
      };

    case "edit_file":
      return {
        verb: "Editing",
        target: file || "file",
        meta: "",
        icon: "rename",
        kind: "edit",
      };

    case "find_and_replace_many":
      return {
        verb: "Replacing in",
        target: file || "file",
        meta: truncate(text(args?.find), 28),
        icon: "rename",
        kind: "edit",
      };

    case "execute":
      return {
        verb: "Running",
        target: truncate(text(args?.command), 64) || "shell command",
        meta: "",
        icon: "terminal",
        kind: "run",
      };

    case "read_output":
      return {
        verb: "Reading output",
        target: text(args?.session_id) ? `[${text(args?.session_id)}]` : "terminal",
        meta: "",
        icon: "terminal",
        kind: "run",
      };

    case "send_input":
      return {
        verb: "Sending input",
        target: text(args?.session_id) ? `→ [${text(args?.session_id)}]` : "terminal",
        meta: truncate(text(args?.text), 28),
        icon: "terminal",
        kind: "run",
      };

    case "terminate_terminal":
      return {
        verb: "Stopping",
        target: text(args?.session_id) ? `[${text(args?.session_id)}]` : "terminal",
        meta: "",
        icon: "stop",
        kind: "run",
      };

    case "find":
    case "search":
      return {
        verb: "Searching",
        target: truncate(text(args?.pattern ?? args?.query), 46) || "codebase",
        meta: text(args?.scope) && text(args?.scope) !== "codebase" ? text(args?.scope) : "",
        icon: "search",
        kind: "search",
      };

    case "semantic_search": {
      const target = asRecord(args?.target);
      return {
        verb: "Searching",
        target: truncate(text(args?.query ?? target?.query), 46) || "semantic index",
        meta: "semantic",
        icon: "cpu",
        kind: "search",
      };
    }

    case "search_web":
      return {
        verb: "Searching web",
        target: truncate(text(args?.query ?? args?.url), 46) || "the web",
        meta: text(args?.domain),
        icon: "globe",
        kind: "web",
      };

    case "ask_user":
      return {
        verb: "Asking you",
        target: truncate(text(args?.question), 52) || "a question",
        meta: "",
        icon: "info",
        kind: "control",
      };

    case "task":
      return {
        verb: args?.finish === false ? "Checking in" : "Finishing",
        target: "",
        meta: "",
        icon: "check",
        kind: "control",
        // Completion sentinel, not user-visible work.
        hidden: true,
      };

    case "archive_context":
      return { verb: "Compacting", target: "context", meta: "", icon: "files", kind: "control" };

    case "retry_block":
      return { verb: "Retrying", target: "file operation", meta: "", icon: "refresh", kind: "edit" };

    case "spawn_subagent":
      return {
        verb: "Delegating",
        target: truncate(text(args?.task ?? args?.task_summary), 46) || "to a sub-agent",
        meta: "",
        icon: "agent",
        kind: "agent",
      };

    case "await_subagent":
      return {
        verb: "Waiting for",
        target: text(args?.agent_id) ? `sub-agent #${text(args?.agent_id)}` : "sub-agent",
        meta: "",
        icon: "clock",
        kind: "agent",
      };

    case "mcp": {
      const server = text(args?.server);
      const tool = text(args?.tool);
      return {
        verb: "Calling",
        target: server && tool ? `${server}/${tool}` : server || tool || "MCP tool",
        meta: "mcp",
        icon: "server",
        kind: "control",
      };
    }

    case "block":
      return {
        verb: text(args?.mode) === "write" ? "Writing" : "Editing",
        target: file || "file",
        meta: "",
        icon: "rename",
        kind: "edit",
      };

    default:
      return {
        verb: prettyToolName(name),
        target: "",
        meta: "",
        icon: "wrench",
        kind: "control",
      };
  }
}

function prettyToolName(name: string): string {
  return name
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

// ------------------------------------------------------------ result parsing

/** True when a tool result represents a failure rather than output. */
export function isErrorResult(result: string): boolean {
  const head = result.slice(0, 300);
  return (
    /^\[[\w:.-]+ ERROR\]/i.test(head) ||
    /^ERROR:/i.test(head) ||
    /^Error:/i.test(head) ||
    /^Failed to /i.test(head) ||
    /^Timeout:/i.test(head) ||
    /\bREJECTED\b/.test(head) ||
    /Permission denied/i.test(head) ||
    /\bTraceback \(most recent call last\)/.test(head)
  );
}

function firstLine(value: string): string {
  return value.split("\n").find((line) => line.trim().length > 0)?.trim() ?? "";
}

/** Fold a raw result into compact metadata on the record. */
export function enrichToolRecord(record: ToolRecord, result: string): ToolRecord {
  record.result = result;
  record.state = isErrorResult(result) ? "error" : "ok";
  record.endedAt = Date.now();

  if (record.state === "error") {
    record.descriptor.meta = truncate(
      firstLine(result).replace(/^\[[\w:.-]+ ERROR\]\s*/i, ""),
      34,
    );
    return record;
  }

  switch (record.name) {
    case "write_file": {
      const lines = result.match(/(\d+)\s+lines?/);
      if (lines) record.descriptor.meta = `${lines[1]} lines`;
      break;
    }
    case "edit_file":
    case "find_and_replace_many": {
      const at = result.match(/\blines?\s+(\d+)(?:\s*-\s*(\d+))?/i);
      if (at) {
        record.descriptor.meta = at[2] ? `L${at[1]}–${at[2]}` : `L${at[1]}`;
        break;
      }
      const occurrences = result.match(/replaced\s+(\d+)\s+occurrence/i);
      if (occurrences) {
        record.descriptor.meta = `${occurrences[1]}×`;
        break;
      }
      const replaced = result.match(/replaced\s+(\d+)-line region with\s+(\d+)\s+lines/i);
      if (replaced) {
        record.descriptor.meta = `−${replaced[1]} +${replaced[2]} lines`;
      } else {
        const total = result.match(/File now has\s+(\d+)\s+lines/i);
        if (total) record.descriptor.meta = `${total[1]} lines`;
      }
      break;
    }
    case "execute": {
      const code = result.match(/return_code:\s*(-?\d+)/);
      if (code) record.descriptor.meta = `exit ${code[1]}`;
      else if (/status:\s*running/i.test(result)) record.descriptor.meta = "still running";
      break;
    }
    case "find": {
      const matches = result.split("\n").filter((line) => line.trim().length > 0).length;
      if (matches > 0) record.descriptor.meta = `${matches} ${matches === 1 ? "match" : "matches"}`;
      break;
    }
    case "semantic_search": {
      const matches = result.match(/(\d+)\s+(?:result|match)/i);
      if (matches) record.descriptor.meta = `${matches[1]} hits`;
      break;
    }
    case "task":
      record.descriptor.meta = "done";
      break;
    case "view_file": {
      const numbered = result.match(/^\s*1\s*[│|:]/m);
      if (numbered) record.descriptor.meta = record.descriptor.meta || "read";
      break;
    }
  }

  return record;
}

// ------------------------------------------------------------- presentation

/** Text used in the activity-dock header: "Editing app.ts · −3 +5 lines". */
export function activitySummary(record: ToolRecord): string {
  const { verb, target, meta } = record.descriptor;
  const head = [verb, target].filter(Boolean).join(" ");
  return meta ? `${head} · ${meta}` : head;
}

/** Compact count breakdown for the turn summary: "2 reads · 1 edit · 1 run". */
export function kindBreakdown(records: ToolRecord[]): string {
  const counts = new Map<ToolKind, number>();
  for (const record of records) {
    counts.set(record.descriptor.kind, (counts.get(record.descriptor.kind) ?? 0) + 1);
  }
  const order: ToolKind[] = ["read", "write", "edit", "run", "search", "web", "agent", "control"];
  const labels: Record<ToolKind, [string, string]> = {
    read: ["read", "reads"],
    write: ["write", "writes"],
    edit: ["edit", "edits"],
    run: ["run", "runs"],
    search: ["search", "searches"],
    web: ["web lookup", "web lookups"],
    agent: ["sub-agent", "sub-agents"],
    control: ["step", "steps"],
  };
  return order
    .filter((kind) => counts.has(kind))
    .map((kind) => {
      const n = counts.get(kind)!;
      return `${n} ${labels[kind][n === 1 ? 0 : 1]}`;
    })
    .join(" · ");
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${String(rest).padStart(2, "0")}s`;
}

/** Parse tool-call arguments that arrive as a JSON string (or already parsed). */
export function parseToolArguments(raw: unknown): Record<string, unknown> | null {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const parsed = JSON.parse(raw);
    return asRecord(parsed);
  } catch {
    return null;
  }
}

/** Best-effort args for legacy `tool(key="value")` lines in old sessions. */
export function parseLegacyArguments(line: string): Record<string, unknown> | null {
  const open = line.indexOf("(");
  const close = line.lastIndexOf(")");
  if (open === -1 || close <= open) return null;
  const body = line.slice(open + 1, close);
  const args: Record<string, unknown> = {};
  const re = /([a-zA-Z_][\w]*)\s*=\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^,]+)/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(body))) {
    let value: unknown = match[2].trim();
    if (typeof value === "string" && /^["']/.test(value)) {
      value = value.slice(1, -1).replace(/\\"/g, '"').replace(/\\n/g, "\n");
    } else if (typeof value === "string" && /^-?\d+$/.test(value)) {
      value = Number(value);
    }
    args[match[1]] = value;
  }
  return Object.keys(args).length ? args : null;
}
