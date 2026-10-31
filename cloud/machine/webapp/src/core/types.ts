// ------------------------------------------------------------------------
// Types mirror the wire protocols exactly:
//   - /ws/control  -> runner/server/src/routes.rs (handle_control_socket)
//   - /ws/agent    -> runner/agent_server.py (NDJSON bridged verbatim)
// Keep these in lockstep with the server; this file is the single source
// of truth for what the client believes the protocol is.
// ------------------------------------------------------------------------

export interface FsEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number;
  modified: number;
}

// Shape of the `system_info` reply — see runner/server/src/sysinfo.rs.
// Every field is individually nullable: each probe there is best-effort, so a
// sandbox with no egress, no pip, or no cgroup v2 still returns a renderable
// payload rather than an error.
export interface SystemInfo {
  server: {
    /** Version of the binary that answered this request. */
    version: string;
    /** "build" when injected at image-build time, "crate" for a dev `cargo run`. */
    source: "build" | "crate";
    commit: string | null;
    /** Release artifact built for this machine, e.g. "linux-amd64". */
    target: string;
    latest: {
      tag: string;
      name: string | null;
      url: string;
      published_at: string | null;
      assets: { name: string; size: number }[];
      matching_asset: { name: string; size: number } | null;
    } | null;
    latest_error: string | null;
    /** null when either version isn't a plain numeric version (dev builds). */
    update_available: boolean | null;
  };
  runtime: {
    package: string;
    version: string | null;
    location: string | null;
    python: string | null;
  };
  sandbox: {
    cpu_model: string | null;
    cpu_cores: number | null;
    cpu_quota_cores: number | null;
    cpuset: string | null;
    memory_limit_bytes: number | null;
    memory_used_bytes: number | null;
    memory_free_bytes: number | null;
    memory_peak_bytes: number | null;
    memory_source: "cgroup" | "proc" | "none";
    host_memory_total_bytes: number | null;
    workspace_path: string;
    containerized: boolean;
  };
  environment: { key: string; value: string }[];
  repository: string;
}

export type ControlRequest =
  | { id: string; type: "list_dir"; path: string }
  | { id: string; type: "read_file"; path: string }
  | { id: string; type: "write_file"; path: string; content: string }
  | { id: string; type: "create_file"; path: string }
  | { id: string; type: "delete_file"; path: string }
  | { id: string; type: "create_dir"; path: string }
  | { id: string; type: "delete_dir"; path: string }
  | { id: string; type: "git_clone"; url: string }
  | { id: string; type: "system_info" }
  | { id: string; type: "ping" };

export type ControlResponse =
  | { id: string; type: "dir_list"; path: string; data: FsEntry[]; success: true }
  | { id: string; type: "file_content"; path: string; content: string; success: true }
  | { id: string; type: "operation_result"; success: true }
  | { id: string; type: "system_info"; data: SystemInfo; success: true }
  | { id: string; type: "error"; error: string; success: false }
  | { id: string; type: "pong" }
  | { type: "file_created"; path: string }
  | { type: "file_deleted"; path: string }
  | { type: "file_changed"; path: string };

// Lifecycle broadcasts from /tmp/codepilot_control.sock, fanned out over the
// same /ws/control channel as fs traffic above but keyed by `event`, not
// `type` — kept as a distinct shape rather than folded into ControlResponse
// so a stray fs message can never be mistaken for a terminal lifecycle one.
export interface TerminalCreatedEvent {
  event: "terminal_created";
  session_id: string;
  socket_path: string;
}

// ---------------------------------------------------------------- agent

export type AgentOutbound =
  | { type: "task"; text: string }
  | { type: "event"; event_type: "abort" }
  | { type: "event"; event_type: "permission_response"; value: boolean }
  | { type: "event"; event_type: "ask_user_response"; value: string }
  | { type: "event"; event_type: "suspend" }
  | { type: "event"; event_type: "new_session"; session_id: string }
  | { type: "event"; event_type: "switch_session"; session_id: string }
  | { type: "event"; event_type: "delete_session"; session_id: string }
  | { type: "event"; event_type: "list_sessions" }
  | { type: "event"; event_type: "update_settings"; settings: AgentSettingsPatch }
  | { type: "event"; event_type: "get_settings" };

export type AgentInbound =
  | { type: "stream"; text: string }
  | { type: "run_started" }
  | { type: "finish" }
  | { type: "tool_call"; tool: string; args: unknown; label?: string }
  | { type: "tool_result"; tool: string; result: unknown }
  | { type: "ask_user"; question: string }
  | { type: "queued_message"; message: string }
  | { type: "inject"; message: string }
  | { type: "permission_request"; tool: string; description: string }
  | { type: "error"; message: string }
  | { type: "sessions_list"; sessions: any[]; active_session_id: string }
  | { type: "session_switched"; session_id: string; history?: any[] }
  | { type: "session_deleted"; session_id: string }
  | { type: "settings_updated"; success: boolean; message?: string }
  | { type: "settings_data"; settings: AgentSettingsData }
  | { type: "context_maintenance"; message: string; stress_pct: number; history_tokens: number; safe_budget: number; candidates: string };

// --------------------------------------------------------- model catalog

export interface ModelDef {
  name: string;
  maxOutputTokens: number;
  contextWindow: number;
  thinkingMode: string;
  reasoningEffortSupport: boolean;
  reasoningEffortLevels: string[];
  pricing: { input: number; output: number };
  notes: string;
}

export const MODEL_CATALOG: Record<string, ModelDef[]> = {
  anthropic: [
    { name: "claude-opus-5", maxOutputTokens: 128000, contextWindow: 1000000, thinkingMode: "adaptive", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high", "extra_high", "max"], pricing: { input: 5.00, output: 25.00 }, notes: "Flagship Opus-tier" },
    { name: "claude-fable-5", maxOutputTokens: 128000, contextWindow: 1000000, thinkingMode: "adaptive (always-on)", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high", "extra_high", "max"], pricing: { input: 10.00, output: 50.00 }, notes: "Most capable Mythos-class model" },
    { name: "claude-sonnet-5", maxOutputTokens: 128000, contextWindow: 1000000, thinkingMode: "adaptive", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high", "extra_high", "max"], pricing: { input: 2.00, output: 10.00 }, notes: "Near-Opus at Sonnet price" },
  ],
  openai: [
    { name: "gpt-5.6-sol", maxOutputTokens: 128000, contextWindow: 1050000, thinkingMode: "reasoning", reasoningEffortSupport: true, reasoningEffortLevels: ["none", "low", "medium", "high", "max"], pricing: { input: 5.00, output: 30.00 }, notes: "Flagship for tool use" },
    { name: "gpt-5.6-terra", maxOutputTokens: 128000, contextWindow: 1050000, thinkingMode: "reasoning", reasoningEffortSupport: true, reasoningEffortLevels: ["none", "low", "medium", "high", "max"], pricing: { input: 2.00, output: 12.00 }, notes: "Balanced everyday coding" },
    { name: "gpt-5.3-codex", maxOutputTokens: 128000, contextWindow: 400000, thinkingMode: "reasoning", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high"], pricing: { input: 5.00, output: 30.00 }, notes: "Coding specialist" },
  ],
  deepseek: [
    { name: "deepseek-v4-pro", maxOutputTokens: 384000, contextWindow: 1000000, thinkingMode: "switchable", reasoningEffortSupport: false, reasoningEffortLevels: [], pricing: { input: 0.435, output: 0.87 }, notes: "Flagship open-weights (MIT)" },
    { name: "deepseek-v4-flash", maxOutputTokens: 384000, contextWindow: 1000000, thinkingMode: "switchable", reasoningEffortSupport: false, reasoningEffortLevels: [], pricing: { input: 0.14, output: 0.28 }, notes: "Cost-efficient tier" },
  ],
  alibaba: [
    { name: "qwen3.8-max", maxOutputTokens: 128000, contextWindow: 1000000, thinkingMode: "switchable", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high"], pricing: { input: 2.00, output: 6.00 }, notes: "Latest Alibaba flagship" },
    { name: "qwen3.7-plus", maxOutputTokens: 65536, contextWindow: 1000000, thinkingMode: "switchable", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high", "max"], pricing: { input: 0.40, output: 1.60 }, notes: "Mid-tier multimodal" },
    { name: "qwen3.7-flash", maxOutputTokens: 65536, contextWindow: 1000000, thinkingMode: "switchable", reasoningEffortSupport: true, reasoningEffortLevels: ["none", "low", "medium", "high", "max"], pricing: { input: 0.03, output: 0.13 }, notes: "Low-cost Flash tier" },
  ],
  experientiallabs: [
    { name: "qwen3.8-27b", maxOutputTokens: 16384, contextWindow: 1000000, thinkingMode: "none", reasoningEffortSupport: false, reasoningEffortLevels: [], pricing: { input: 0.00, output: 0.00 }, notes: "FREE · Fast 27B — best value default" },
    { name: "deepseek-v4-flash", maxOutputTokens: 32768, contextWindow: 1050000, thinkingMode: "none", reasoningEffortSupport: false, reasoningEffortLevels: [], pricing: { input: 0.00, output: 0.00 }, notes: "FREE · DeepSeek V4 Flash via gateway" },
    { name: "gpt-5.0-luna", maxOutputTokens: 32768, contextWindow: 1050000, thinkingMode: "none", reasoningEffortSupport: false, reasoningEffortLevels: [], pricing: { input: 0.00, output: 0.00 }, notes: "FREE · GPT-5 Luna via gateway" },
    { name: "claude-sonnet-5", maxOutputTokens: 128000, contextWindow: 1000000, thinkingMode: "adaptive", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high"], pricing: { input: 2.00, output: 10.00 }, notes: "Claude Sonnet 5 via gateway" },
    { name: "claude-opus-5", maxOutputTokens: 128000, contextWindow: 1000000, thinkingMode: "adaptive", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high"], pricing: { input: 5.00, output: 25.00 }, notes: "Claude Opus 5 via gateway" },
    { name: "kimi-k3", maxOutputTokens: 32768, contextWindow: 1050000, thinkingMode: "reasoning", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high"], pricing: { input: 4.00, output: 15.00 }, notes: "Kimi K3 · strong reasoning" },
    { name: "gpt-5.6-sol", maxOutputTokens: 128000, contextWindow: 1050000, thinkingMode: "reasoning", reasoningEffortSupport: true, reasoningEffortLevels: ["low", "medium", "high"], pricing: { input: 2.00, output: 10.00 }, notes: "GPT-5.6 Sol · flagship reasoning" },
  ],
  // These lists are suggestions, not constraints — the model field is a
  // free-text combobox so any LiteLLM model ID works.
  gemini: [],
  openrouter: [],
  ollama: [],
  openai_compatible: [],
  litellm: [],
};


export const PROVIDER_API_KEY_ENV: Record<string, string> = {
  anthropic: "ANTHROPIC_API_KEY",
  openai: "OPENAI_API_KEY",
  gemini: "GEMINI_API_KEY",
  deepseek: "DEEPSEEK_API_KEY",
  alibaba: "DASHSCOPE_API_KEY",
  openrouter: "OPENROUTER_API_KEY",
  experientiallabs: "EXPERIENTIAL_API_KEY",
  ollama: "OLLAMA_API_KEY",
  openai_compatible: "LLM_API_KEY",
  litellm: "LLM_API_KEY",
};


export const PROVIDER_LABELS: Record<string, string> = {
  experientiallabs: "Experiential Labs",
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Google Gemini",
  deepseek: "Deepseek",
  alibaba: "Alibaba (Qwen)",
  openrouter: "OpenRouter",
  ollama: "Ollama (local)",
  openai_compatible: "Custom / OpenAI-compatible",
  litellm: "Raw LiteLLM route",
};


// --------------------------------------------------------- settings patch

export interface McpServerDef {
  name: string;
  url: string;
  api_key_env: string;
  api_key_param: string;
  api_key?: string; // user-supplied value (kept in-memory in the settings form, never round-tripped from the server)
}

export interface EmbeddingConfig {
  model: string;
  base_url: string;
  api_key_env: string;
  api_key?: string; // user-supplied value
}

export interface ToolConfig {
  name: string;
  enabled: boolean;
  require_permission?: boolean;
  config?: Record<string, unknown>;
}

// Mirrors agent.yaml's shape closely enough for the settings form.
// Only fields the form actually edits are patched to the server on save.
export interface AgentSettingsPatch {
  provider?: string;
  model?: string;
  temperature?: number;
  max_tokens?: number;
  api_key_env?: string;
  api_key?: string;
  api_base?: string;
  api_version?: string;
  thinking?: { enabled: boolean; budget_tokens?: number; reasoning_effort?: string };
  max_steps?: number;
  unsafe_mode?: boolean;
  sub_agents?: { enabled: boolean; max_steps?: number };
  memory?: { max_context_tokens?: number; context_stress_multiplier?: number; context_stress_trigger?: number };
  tools?: Record<string, { enabled?: boolean; require_permission?: boolean }>;
  system_prompt_append?: string;
  system_prompt_mode?: "inline" | "file";
  embedding?: EmbeddingConfig;
  semantic_search_enabled?: boolean;
  mcp?: {
    enabled: boolean;
    servers: McpServerDef[];
    embedding_model?: string;
    embedding_api_key_env?: string;
    embedding_base_url?: string;
    embedding_api_key?: string;
  };
}

// Full settings data returned by get_settings event
export interface AgentSettingsData {
  provider: string;
  model: string;
  temperature: number;
  max_tokens: number;
  api_key_env: string;
  api_base: string;
  api_version: string;
  thinking: { enabled: boolean; budget_tokens?: number; reasoning_effort?: string };
  max_steps: number;
  unsafe_mode: boolean;
  sub_agents: { enabled: boolean; max_steps: number };
  memory: { max_context_tokens: number; context_stress_multiplier: number; context_stress_trigger: number };
  system_prompt: string;
  system_prompt_mode: "inline" | "file";
  system_prompt_custom: string; // inline mode only: the user's custom text, with the fixed default prefix already stripped off
  instructions_file_path: string; // absolute path to the file used when system_prompt_mode === "file"
  tools: ToolConfig[];
  embedding: EmbeddingConfig;
  semantic_search_enabled: boolean;
  mcp_enabled: boolean;
  mcp_servers: McpServerDef[];
}

export interface EditorSettings {
  theme: "dark" | "light";
  fontSize: number;
  tabSize: number;
  wordWrap: boolean;
  minimap: boolean;
  formatOnSave: boolean;
}
