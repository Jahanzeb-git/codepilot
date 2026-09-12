"""
agent_server.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: Low-latency Unix domain socket IPC server managing the CodePilot AI Agent Runtime lifecycle and NDJSON event streaming.
Licensed: MIT
"""
from codepilot import (
    Runtime,
    on_stream,
    on_finish,
    on_tool_call,
    on_tool_result,
    on_ask_user,
    on_user_message_queued,
    on_user_message_injected,
    on_permission_request,
    on_thinking_stream,
    on_runtime_error,
    on_context_maintenance_start,
    EventType,
)

import os
import socket
import threading
import json
import queue
import urllib
import yaml

SESSIONS_DIR       = os.path.expanduser("~/.codepilot/sessions")
AGENT_YAML_PATH    = "/opt/codepilot/agent.yaml"
# Co-located with agent.yaml (not ~/.codepilot) so the relative path written
# into agent.yaml's `system_prompt: "./prompts/instructions.md"` and this
# constant always point at the exact same file.
INSTRUCTIONS_FILE  = os.path.join(os.path.dirname(AGENT_YAML_PATH), "prompts", "instructions.md")

# The fixed baseline system prompt shipped in agent.yaml. Used as the anchor
# for "inline" custom-instructions mode: every save recomputes
# DEFAULT_SYSTEM_PROMPT + custom_text from scratch instead of concatenating
# onto whatever happens to already be on disk, which is what caused custom
# instructions to duplicate on every single Save & Restart.
DEFAULT_SYSTEM_PROMPT = (
    "You're Codepilot agent interfaced through IDE on user browser "
    "developed by 'Jahanzeb Ahmed <jahanzebahmed.xyz>'."
)

os.makedirs(SESSIONS_DIR, exist_ok=True)

_MACHINE_SECRET       = os.environ.pop("MACHINE_SECRET",       "")
_CONTROL_PLANE_URL    = os.environ.pop("CONTROL_PLANE_URL",    "")
_EXPERIENTIAL_API_KEY = os.environ.pop("EXPERIENTIAL_API_KEY", "")
_DASHSCOPE_API_KEY    = os.environ.pop("DASHSCOPE_API_KEY",    "")
_ALIBABA_API_KEY      = os.environ.pop("ALIBABA_API_KEY",      "")
_VOYAGE_API_KEY       = os.environ.pop("VOYAGE_API_KEY",       "")
_TAVILY_API_KEY       = os.environ.pop("TAVILY_API_KEY",       "")

# Tracks user-supplied API key override (from update_settings).
# When a user brings their own LLM key, this replaces the platform default
# for the duration of that session.
_user_llm_env_var: str = ""   # e.g. "DASHSCOPE_API_KEY"
_user_llm_key:     str = ""   # the user's own key value
_user_runtime_keys: dict[str, str] = {}

# ---web search tool registration---
from web_search_tool import register_search_web_tool, set_tavily_key
set_tavily_key(_TAVILY_API_KEY)

# ---Socket setup---
SOCKET_PATH = "/tmp/agent_runtime.sock"

if os.path.exists(SOCKET_PATH):
    os.unlink(SOCKET_PATH)

print("[INFO] Creating unix socket...")
server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

print(f"[INFO] Binding to {SOCKET_PATH}")
server_socket.bind(SOCKET_PATH)

print("[INFO] Listening for connections...")
server_socket.listen(1)


def generate_session_id(dir: str) -> str:
    if not os.listdir(dir):
        return "session_001"
    files = [
        f for f in os.listdir(dir)
        if os.path.isfile(os.path.join(dir, f))
        and f.startswith("session_") and f.endswith(".json")
    ]
    if not files:
        return "session_001"
    files.sort()
    last_file = files[-1]
    session_num = int(last_file.split("_")[1].split(".")[0]) + 1
    return f"session_{session_num:03d}"


# --- shared mutable state ---
lock = threading.Lock()
_client_socket: socket.socket | None = None
runtime_thread: threading.Thread | None = None
permission_queue: queue.Queue = queue.Queue()
ask_user_queue: queue.Queue = queue.Queue()

# None means the runtime failed to init — will emit error to next connected client
runtime = None
_runtime_init_error: str | None = None


def emit(obj: dict) -> None:
    """Send a NDJSON event to the currently connected extension client."""
    line = (json.dumps(obj) + "\n").encode()
    try:
        with lock:
            if _client_socket is not None:
                _client_socket.sendall(line)
    except (BrokenPipeError, ConnectionResetError, OSError) as e:
        print(f"[ERROR] emit failed, client likely disconnected: {e}")


# ---runtime environment helpers---

def _inject_runtime_env() -> None:
    """
    Temporarily re-inject API keys into os.environ so that Runtime.__init__()
    can read them (the codepilot library reads them from os.environ during
    provider and tool setup).
    """
    # Platform-provided defaults
    if _EXPERIENTIAL_API_KEY:
        os.environ["EXPERIENTIAL_API_KEY"] = _EXPERIENTIAL_API_KEY
    if _DASHSCOPE_API_KEY:
        os.environ["DASHSCOPE_API_KEY"] = _DASHSCOPE_API_KEY
    if _ALIBABA_API_KEY:
        os.environ["ALIBABA_API_KEY"]   = _ALIBABA_API_KEY
    if _VOYAGE_API_KEY:
        os.environ["VOYAGE_API_KEY"]    = _VOYAGE_API_KEY
    if _TAVILY_API_KEY:
        os.environ["TAVILY_API_KEY"]    = _TAVILY_API_KEY

    # User override: if the user brought their own LLM key it takes precedence
    if _user_llm_key and _user_llm_env_var:
        os.environ[_user_llm_env_var] = _user_llm_key
    for env_var, key in _user_runtime_keys.items():
        if env_var and key:
            os.environ[env_var] = key


def _scrub_runtime_env() -> None:
    """
    Remove all API keys from os.environ immediately after Runtime.__init__()
    returns. Must be called before any subprocess can fork.
    """
    for k in ("EXPERIENTIAL_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_API_KEY", "VOYAGE_API_KEY", "TAVILY_API_KEY"):
        os.environ.pop(k, None)
    # Also scrub the user's custom env var name if different from defaults
    if _user_llm_env_var:
        os.environ.pop(_user_llm_env_var, None)
    for env_var in _user_runtime_keys:
        os.environ.pop(env_var, None)


def _warmup_mcp(r: Runtime) -> None:
    """
    codepilot's MCPTools.setup() — the step that actually connects to each
    configured MCP server and reads its api_key from os.environ — is
    deferred to the *first* run() call, because MCPTools.__init__() is
    synchronous and can't await a connection. Runtime() itself never calls
    run(), so under the old inject -> init -> scrub sequence, setup() would
    only ever fire after _scrub_runtime_env() had already wiped the MCP
    server keys out of os.environ — every server key lookup returned None,
    and every server connection came back 401. This is why manually
    exporting the same env var name didn't help either: the timing was
    wrong, not the key.

    Fix: force that same setup() coroutine to run right now, synchronously,
    on the runtime's own event loop, while we're still inside the inject
    window — so each server's api_key_env is actually readable when it's
    looked up.
    """
    async_rt = getattr(r, "_async", None)
    mcp_tools = getattr(async_rt, "_mcp_tools", None) if async_rt is not None else None
    if async_rt is None or mcp_tools is None:
        return
    try:
        r._run_coro(mcp_tools.setup())
        async_rt._mcp_setup_done = True
        print("[INFO] MCP servers connected and indexed.")
    except Exception as e:
        # One unreachable/misconfigured server shouldn't take the whole
        # runtime down — surface it and continue; that server just won't
        # show up in the mcp() tool's index.
        print(f"[ERROR] MCP warmup failed (server(s) unreachable or key invalid): {e}")


def _ensure_tool(data: dict, name: str, enabled: bool = True) -> dict:
    tools = data.setdefault("agent", {}).setdefault("tools", [])
    for tool in tools:
        if tool.get("name") == name:
            tool["enabled"] = enabled
            tool.setdefault("config", {})
            return tool
    tool = {"name": name, "enabled": enabled, "config": {}}
    tools.append(tool)
    return tool


def _ensure_embedding_search_tool(data: dict) -> None:
    semantic = _ensure_tool(data, "semantic_search", True)
    cfg = semantic.setdefault("config", {})
    cfg.setdefault("api_key_env", "VOYAGE_API_KEY")
    cfg.setdefault("model", "voyage-code-3")
    cfg.setdefault("base_url", "https://api.voyageai.com/v1")
    cfg.setdefault("provider", "openai")
    cfg.setdefault("max_results", 5)
    cfg.setdefault("timeout", 60)
    cfg.setdefault("max_output_chars", 8000)


def _apply_mcp_settings(data: dict, settings: dict) -> None:
    mcp_settings = settings.get("mcp")
    if not isinstance(mcp_settings, dict):
        _ensure_embedding_search_tool(data)
        return

    _ensure_embedding_search_tool(data)
    mcp = _ensure_tool(data, "mcp", bool(mcp_settings.get("enabled")))
    cfg = mcp.setdefault("config", {})
    cfg["embedding_model"] = mcp_settings.get("embedding_model") or "voyage-code-3"
    cfg["embedding_api_key_env"] = mcp_settings.get("embedding_api_key_env") or "VOYAGE_API_KEY"
    cfg["embedding_base_url"] = mcp_settings.get("embedding_base_url") or "https://api.voyageai.com/v1"
    cfg["top_k"] = int(mcp_settings.get("top_k") or cfg.get("top_k") or 3)

    servers = []
    for idx, srv in enumerate(mcp_settings.get("servers") or [], start=1):
        if not isinstance(srv, dict):
            continue
        url = str(srv.get("url") or "").strip()
        if not url:
            continue
        name = str(srv.get("name") or f"mcp-server-{idx}").strip()
        api_key_env = str(srv.get("api_key_env") or "").strip()
        api_key_param = str(srv.get("api_key_param") or "").strip()
        item = {"name": name, "url": url}
        if api_key_env:
            item["api_key_env"] = api_key_env
        if api_key_param:
            item["api_key_param"] = api_key_param
        servers.append(item)
    cfg["servers"] = servers

# ---Runtime Initialization---

def init_runtime(session_id: str) -> Runtime:
    """Create a Runtime and bind all event handlers to it."""
    r = Runtime(
        agent_file=AGENT_YAML_PATH,
        stream=True,
        session="file",
        session_id=session_id,
    )
    register_search_web_tool(r)

    # Must happen before secure_init_runtime() scrubs the injected env vars —
    # see _warmup_mcp() docstring for why this can't just be left to the
    # library's normal "connect on first run()" behavior.
    _warmup_mcp(r)

    state = {"in_thinking": False}

    @on_thinking_stream(r)
    def _thinking_stream(thinking: str, **_):
        if not state["in_thinking"]:
            emit({"type": "stream", "text": "<thinking>\n"})
            state["in_thinking"] = True
        emit({"type": "stream", "text": thinking})

    @on_stream(r)
    def _stream(text: str, **_):
        if state["in_thinking"]:
            emit({"type": "stream", "text": "\n</thinking>\n"})
            state["in_thinking"] = False
        print("[EVENT] on_stream called.")
        emit({"type": "stream", "text": text})

    @on_finish(r)
    def _finish(summary: str, **_):
        if state["in_thinking"]:
            emit({"type": "stream", "text": "\n</thinking>\n"})
            state["in_thinking"] = False
        print("[EVENT] on_finish called.")
        emit({"type": "finish"})

    @on_tool_call(r)
    def _tool_call(tool: str, args: dict, label: str = "", **_):
        if state["in_thinking"]:
            emit({"type": "stream", "text": "\n</thinking>\n"})
            state["in_thinking"] = False
        print("[EVENT] on_tool_call called.")
        emit({"type": "tool_call", "tool": tool, "args": args, "label": label})

    @on_tool_result(r)
    def _tool_result(tool: str, result: str, **_):
        print("[EVENT] on_tool_result called.")
        emit({"type": "tool_result", "tool": tool, "result": result})

    @on_ask_user(r)
    def _ask(question: str, **_) -> str:
        if state["in_thinking"]:
            emit({"type": "stream", "text": "\n</thinking>\n"})
            state["in_thinking"] = False
        print("[EVENT] on_ask_user called.")
        emit({"type": "ask_user", "question": question})
        answer = ask_user_queue.get()
        print(f"[EVENT] Received ask_user response: {answer}")
        return answer

    @on_user_message_queued(r)
    def _queued(message: str, **_):
        print("[EVENT] on_user_message_queued called.")
        emit({"type": "queued_message", "message": message})

    @on_user_message_injected(r)
    def _injected(message: str, **_):
        print("[EVENT] on_user_message_injected called.")
        emit({"type": "inject", "message": message})

    @on_permission_request(r)
    def _gate(tool: str, description: str, **_) -> bool:
        print("[EVENT] on_permission_request called.")
        emit({"type": "permission_request", "tool": tool, "description": description})
        allowed = permission_queue.get()
        print(f"[EVENT] Permission {'granted' if allowed else 'denied'} for tool: {tool}")
        return allowed

    @on_runtime_error(r)
    def _runtime_error(error: str, **_):
        if "PARSER ERROR" in error or "EXECUTION ERROR" in error:
            return
        print(f"[EVENT] Runtime error: {error}")
        emit({"type": "error", "message": error})

    @on_context_maintenance_start(r)
    def _context_maintenance(stress_pct: int, history_tokens: int, safe_budget: int, candidates: str, **_):
        print("[EVENT] on_context_maintenance_start called.")
        emit({
            "type": "context_maintenance",
            "message": f"Context maintenance triggered (Stress: {stress_pct}%, Load: {history_tokens:,} / {safe_budget:,} safe tokens).",
            "stress_pct": stress_pct,
            "history_tokens": history_tokens,
            "safe_budget": safe_budget,
            "candidates": candidates
        })

    print(f"[INFO] Runtime initialised for session: {session_id}")
    return r


def try_init_runtime(session_id: str) -> tuple[Runtime | None, str | None]:
    """Safely initialise a runtime. Returns (runtime, None) or (None, error_message)."""
    try:
        return init_runtime(session_id), None
    except (OSError, EnvironmentError) as e:
        msg = f"Configuration error: {e}"
        print(f"[ERROR] {msg}")
        return None, msg
    except Exception as e:
        msg = f"Runtime error: {e}"
        print(f"[ERROR] {msg}")
        return None, msg


def secure_init_runtime(session_id: str) -> tuple[Runtime | None, str | None]:
    """
    Full secure runtime initialisation sequence:
      1. Inject API keys temporarily into os.environ.
      2. Init the runtime (which reads keys and spawns the default terminal).
      3. Immediately scrub keys from os.environ.

    Note: The initial terminal spawned by Runtime() is already secure because 
    codepilot's multiplexer natively uses os.execve() with a filtered clean_env.

    This is the ONLY function that should be called to create a Runtime.
    """
    _inject_runtime_env()
    rt, err = try_init_runtime(session_id)
    _scrub_runtime_env()          # always scrub, even if init failed
    return rt, err


# ---initial runtime boot---
active_session_id = generate_session_id(SESSIONS_DIR)
runtime, _runtime_init_error = secure_init_runtime(active_session_id)

def handle_client(conn, addr):
    global _client_socket, runtime, runtime_thread, active_session_id, _runtime_init_error, _user_llm_env_var, _user_llm_key, _user_runtime_keys

    print(f"[INFO] Client connected: {addr}")

    with lock:
        _client_socket = conn

    # If the initial runtime boot failed, immediately notify the newly connected client
    if runtime is None and _runtime_init_error:
        emit({"type": "error", "message": _runtime_init_error})

    recv_buffer = b""

    # Inner loop: read NDJSON messages from this client
    while True:
        try:
            chunk = conn.recv(4096)
        except (ConnectionResetError, OSError) as e:
            print(f"[ERROR] client connection reset: {e}")
            break

        if not chunk:
            print("[INFO] Client disconnected (EOF).")
            break

        recv_buffer += chunk

        while b"\n" in recv_buffer:
            line, recv_buffer = recv_buffer.split(b"\n", 1)
            if not line.strip():
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[ERROR] Failed to parse NDJSON: {e}")
                continue

            if obj["type"] == "task":
                if runtime is None:
                    emit({"type": "error", "message": _runtime_init_error or "Agent runtime is not initialised. Please check your settings."})
                elif runtime_thread is None or not runtime_thread.is_alive():
                    def _run_with_catch(text):
                        try:
                            runtime.run(text)
                        except Exception as e:
                            print(f"[ERROR] Runtime error:\n{e}")
                            emit({"type": "error", "message": f"LLM provider error: {e}"})

                    runtime_thread = threading.Thread(
                        target=_run_with_catch,
                        args=(obj["text"],),
                        daemon=True,
                    )
                    runtime_thread.start()
                else:
                    try:
                        runtime.send_message(obj["text"])
                    except Exception as e:
                        print(f"[ERROR] Runtime error:\n{e}")
                        emit({"type": "error", "message": f"LLM provider error: {e}"})

            elif obj["type"] == "event" and obj["event_type"] == "abort":
                if runtime:
                    runtime.abort()

            elif obj["type"] == "event" and obj["event_type"] == "permission_response":
                permission_queue.put(obj["value"])

            elif obj["type"] == "event" and obj["event_type"] == "ask_user_response":
                ask_user_queue.put(obj["value"])

            elif obj["type"] == "event" and obj["event_type"] == "suspend":
                if runtime:
                    runtime.abort()
                try:
                    URL = f"{_CONTROL_PLANE_URL}/machines/suspend"
                    req = urllib.request.Request(
                        URL,
                        method="POST",
                        headers={
                            "X-Machine-Secret": _MACHINE_SECRET,
                            "Content-Type": "application/json",
                        },
                    )
                    with urllib.request.urlopen(req) as resp:
                        data = resp.read()
                        print(f"[INFO] machine suspended: status={resp.status} response={json.loads(data)}")
                except Exception as e:
                    print(f"[ERROR] Failed to notify control plane of suspension: {e}")
                break

            elif obj["type"] == "event" and obj["event_type"] in ("new_session", "switch_session"):
                new_rt, err = secure_init_runtime(obj["session_id"])
                if err:
                    emit({"type": "error", "message": err})
                else:
                    active_session_id = obj["session_id"]
                    runtime = new_rt
                    _runtime_init_error = None

                    # Read the session file to emit the history
                    file_path = os.path.join(SESSIONS_DIR, f"{active_session_id}.json")
                    history = []
                    if os.path.exists(file_path):
                        try:
                            with open(file_path, "r", encoding="utf-8") as f:
                                sdata = json.load(f)
                                history = sdata.get("messages", [])
                        except Exception as e:
                            print(f"[ERROR] Failed to read session history: {e}")

                    emit({"type": "session_switched", "session_id": obj["session_id"], "history": history})

            elif obj["type"] == "event" and obj["event_type"] == "delete_session":
                session_to_delete = obj["session_id"]
                file_path = os.path.join(SESSIONS_DIR, f"{session_to_delete}.json")
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    
                    if active_session_id == session_to_delete:
                        runtime = None
                        active_session_id = None
                        _runtime_init_error = None
                        
                    emit({"type": "session_deleted", "session_id": session_to_delete})
                except Exception as e:
                    emit({"type": "error", "message": f"Failed to delete session: {e}"})

            elif obj["type"] == "event" and obj["event_type"] == "list_sessions":
                try:
                    sessions_list = []
                    for fname in os.listdir(SESSIONS_DIR):
                        if not fname.endswith(".json"): continue
                        file_path = os.path.join(SESSIONS_DIR, fname)
                        if not os.path.isfile(file_path): continue
                        
                        try:
                            with open(file_path, "r", encoding="utf-8") as f:
                                sdata = json.load(f)
                                
                            sid = sdata.get("session_id", fname.replace(".json", ""))
                            created = sdata.get("created_at", 0)
                            updated = sdata.get("updated_at", created)
                            agent_name = sdata.get("agent_name", "Codepilot")
                            
                            # Find first user message for snippet
                            snippet = "New Session"
                            for msg in sdata.get("messages", []):
                                if msg.get("role") == "user":
                                    content = str(msg.get("content", ""))
                                    if "[USER INPUT]" in content:
                                        parts = content.split("\n", 1)
                                        if len(parts) > 1:
                                            snippet = parts[1].strip()
                                        else:
                                            snippet = content.replace("[USER INPUT]", "").strip()
                                        break
                                    elif not content.startswith("[SYSTEM]") and not content.startswith("[EXECUTION RESULT]"):
                                        snippet = content.split("\n")[0].strip()
                                        break
                                        
                            sessions_list.append({
                                "session_id": sid,
                                "created_at": created,
                                "updated_at": updated,
                                "agent_name": agent_name,
                                "snippet": snippet[:100] + ("..." if len(snippet) > 100 else "")
                            })
                        except Exception as parse_err:
                            print(f"[ERROR] Failed to parse session {fname}: {parse_err}")
                            
                    # Sort by updated_at descending
                    sessions_list.sort(key=lambda x: x["updated_at"], reverse=True)
                    emit({"type": "sessions_list", "sessions": sessions_list, "active_session_id": active_session_id})
                except Exception as e:
                    emit({"type": "error", "message": f"Failed to list sessions: {e}"})

            elif obj["type"] == "event" and obj["event_type"] == "get_settings":
                try:
                    with open(AGENT_YAML_PATH, "r") as f:
                        data = yaml.safe_load(f)
                    agent = data.get("agent", {})
                    model = agent.get("model", {})
                    rt = agent.get("runtime", {})
                    sub = agent.get("sub_agents", {})
                    mem = agent.get("memory", {})
                    thinking = model.get("thinking", {})
                    tools_raw = agent.get("tools", [])

                    tool_list = []
                    for t in tools_raw:
                        tool_list.append({
                            "name": t.get("name", ""),
                            "enabled": t.get("enabled", True),
                            "require_permission": t.get("config", {}).get("require_permission", False),
                        })

                    # Extract embedding config from semantic_search tool
                    sem_tool = next((t for t in tools_raw if t.get("name") == "semantic_search"), None)
                    sem_cfg = (sem_tool or {}).get("config", {})
                    mcp_tool = next((t for t in tools_raw if t.get("name") == "mcp"), None)
                    mcp_cfg = (mcp_tool or {}).get("config", {})

                    raw_prompt = str(agent.get("system_prompt", ""))
                    is_file_mode = raw_prompt.endswith((".md", ".txt", ".j2"))
                    if is_file_mode:
                        custom_text = ""
                    elif raw_prompt.startswith(DEFAULT_SYSTEM_PROMPT):
                        custom_text = raw_prompt[len(DEFAULT_SYSTEM_PROMPT):].strip()
                    else:
                        # system_prompt was fully replaced by something that
                        # doesn't start with our known default (e.g. hand-
                        # edited YAML) — show it as-is rather than guessing.
                        custom_text = raw_prompt

                    emit({
                        "type": "settings_data",
                        "settings": {
                            "provider": model.get("provider", "alibaba"),
                            "model": model.get("name", ""),
                            "temperature": model.get("temperature", 1.0),
                            "max_tokens": model.get("max_tokens", 65536),
                            "api_key_env": model.get("api_key_env", ""),
                            "thinking": {
                                "enabled": thinking.get("enabled", False),
                                "budget_tokens": thinking.get("budget_tokens"),
                                "reasoning_effort": thinking.get("reasoning_effort"),
                            },
                            "max_steps": rt.get("max_steps", 35),
                            "unsafe_mode": rt.get("unsafe_mode", True),
                            "sub_agents": {
                                "enabled": sub.get("enabled", False),
                                "max_steps": sub.get("max_steps", 20),
                            },
                            "memory": {
                                "max_context_tokens": mem.get("max_context_tokens", 120000),
                                "context_stress_multiplier": mem.get("context_stress_multiplier", 1.0),
                                "context_stress_trigger": mem.get("context_stress_trigger", 0.78),
                            },
                            "system_prompt": raw_prompt,
                            "system_prompt_mode": "file" if is_file_mode else "inline",
                            "system_prompt_custom": custom_text,
                            "instructions_file_path": INSTRUCTIONS_FILE,
                            "tools": tool_list,
                            "embedding": {
                                "model": sem_cfg.get("model", "voyage-code-3"),
                                "base_url": sem_cfg.get("base_url", "https://api.voyageai.com/v1"),
                                "api_key_env": sem_cfg.get("api_key_env", "VOYAGE_API_KEY"),
                            },
                            "semantic_search_enabled": (sem_tool or {}).get("enabled", True),
                            "mcp_enabled": (mcp_tool or {}).get("enabled", False),
                            "mcp_servers": mcp_cfg.get("servers", []),
                        },
                    })
                except Exception as e:
                    print(f"[ERROR] get_settings failed: {e}")
                    emit({"type": "error", "message": f"Failed to read settings: {e}"})

            elif obj["type"] == "event" and obj["event_type"] == "update_settings":

                with open(AGENT_YAML_PATH, "r") as f:
                    data = yaml.safe_load(f)

                settings = obj.get("settings", {})

                def update_if_present(target_dict, key, source_dict, source_key=None):
                    skey = source_key or key
                    val = source_dict.get(skey)
                    if val not in (None, "", [], {}):
                        target_dict[key] = val

                update_if_present(data["agent"]["model"], "provider",    settings)
                update_if_present(data["agent"]["model"], "name",        settings, "model")
                update_if_present(data["agent"]["model"], "temperature",  settings)
                update_if_present(data["agent"]["model"], "max_tokens",   settings)
                update_if_present(data["agent"]["model"], "api_key_env",  settings)

                # Thinking config
                thinking_settings = settings.get("thinking", {})
                if thinking_settings:
                    if "thinking" not in data["agent"]["model"]:
                        data["agent"]["model"]["thinking"] = {}
                    thinking_block = data["agent"]["model"]["thinking"]
                    if "enabled" in thinking_settings:
                        thinking_block["enabled"] = thinking_settings["enabled"]
                    if thinking_settings.get("enabled"):
                        if thinking_settings.get("budget_tokens"):
                            thinking_block["budget_tokens"] = int(thinking_settings["budget_tokens"])
                        if thinking_settings.get("reasoning_effort"):
                            thinking_block["reasoning_effort"] = thinking_settings["reasoning_effort"]
                    else:
                        data["agent"]["model"]["thinking"] = {"enabled": False}

                update_if_present(data["agent"]["runtime"], "max_steps",    settings)
                update_if_present(data["agent"]["runtime"], "unsafe_mode",  settings)

                mem_settings = settings.get("memory", {})
                if mem_settings:
                    if "memory" not in data["agent"]:
                        data["agent"]["memory"] = {}
                    mem_block = data["agent"]["memory"]
                    if "max_context_tokens" in mem_settings:
                        mem_block["max_context_tokens"] = int(mem_settings["max_context_tokens"])
                    if "context_stress_multiplier" in mem_settings:
                        mem_block["context_stress_multiplier"] = float(mem_settings["context_stress_multiplier"])
                    if "context_stress_trigger" in mem_settings:
                        mem_block["context_stress_trigger"] = float(mem_settings["context_stress_trigger"])

                _apply_mcp_settings(data, settings)

                # Sub-agents config
                sub_agents_settings = settings.get("sub_agents", {})
                if sub_agents_settings:
                    if "sub_agents" not in data["agent"]:
                        data["agent"]["sub_agents"] = {}
                    sub_agents_block = data["agent"]["sub_agents"]
                    if "enabled" in sub_agents_settings:
                        sub_agents_block["enabled"] = sub_agents_settings["enabled"]
                    if "max_steps" in sub_agents_settings:
                        sub_agents_block["max_steps"] = int(sub_agents_settings["max_steps"])

                if "tools" in settings and settings["tools"]:
                    for tool_name, tool_conf in settings["tools"].items():
                        for t in data["agent"]["tools"]:
                            if t["name"] == tool_name:
                                for k, v in tool_conf.items():
                                    if v not in (None, "", [], {}):
                                        if k == "require_permission":
                                            t.setdefault("config", {})
                                            t["config"][k] = v
                                        else:
                                            t[k] = v

                if "system_prompt_mode" in settings or "system_prompt_append" in settings:
                    mode = str(settings.get("system_prompt_mode") or "inline").strip()
                    if mode == "file":
                        rel_path = "./prompts/instructions.md"
                        data["agent"]["system_prompt"] = rel_path
                        # Make sure the file actually exists so the agent's
                        # next Runtime init and the "Open in Editor" button
                        # both find something real instead of a dangling path.
                        os.makedirs(os.path.dirname(INSTRUCTIONS_FILE), exist_ok=True)
                        if not os.path.exists(INSTRUCTIONS_FILE):
                            with open(INSTRUCTIONS_FILE, "w") as f:
                                f.write(DEFAULT_SYSTEM_PROMPT + "\n")
                    else:
                        # Recompute from the fixed baseline every time instead
                        # of appending onto whatever's already on disk — this
                        # is what was causing the same text to pile up on
                        # every Save & Restart even with no edits.
                        custom = str(settings.get("system_prompt_append") or "").strip()
                        data["agent"]["system_prompt"] = (
                            DEFAULT_SYSTEM_PROMPT + ("\n\n" + custom if custom else "")
                        )

                with open(AGENT_YAML_PATH, "w") as f:
                    yaml.safe_dump(data, f, sort_keys=False)

                # If the user is bringing their own API key, record it so
                # _inject_runtime_env() / _scrub_runtime_env() use it correctly.
                new_api_key  = settings.get("api_key", "").strip()
                new_env_var  = settings.get("api_key_env", "").strip()
                if new_api_key and new_env_var:
                    _user_llm_env_var = new_env_var
                    _user_llm_key     = new_api_key
                    print(f"[INFO] User-supplied key recorded for {new_env_var}")

                mcp_settings = settings.get("mcp", {})
                if isinstance(mcp_settings, dict):
                    embedding_key = str(mcp_settings.get("embedding_api_key") or "").strip()
                    embedding_env = str(mcp_settings.get("embedding_api_key_env") or "VOYAGE_API_KEY").strip()
                    if embedding_key and embedding_env:
                        _user_runtime_keys[embedding_env] = embedding_key
                        print(f"[INFO] User-supplied MCP embedding key recorded for {embedding_env}")
                    for srv in mcp_settings.get("servers") or []:
                        if not isinstance(srv, dict):
                            continue
                        server_key = str(srv.get("api_key") or "").strip()
                        server_env = str(srv.get("api_key_env") or "").strip()
                        if server_key and server_env:
                            _user_runtime_keys[server_env] = server_key
                            print(f"[INFO] User-supplied MCP server key recorded for {server_env}")

                # Preserve the current session ID so conversation is not lost
                curr_sid = active_session_id
                if runtime and hasattr(runtime, "cleanup"):
                    try:
                        runtime.cleanup()
                    except Exception:
                        pass

                # secure_init_runtime handles inject → init → scrub → restart
                new_rt, err = secure_init_runtime(curr_sid)

                if err:
                    runtime = None
                    _runtime_init_error = err
                    emit({"type": "settings_updated", "success": False, "message": err})
                else:
                    runtime = new_rt
                    _runtime_init_error = None
                    emit({"type": "settings_updated", "success": True})

    with lock:
        if _client_socket == conn:
            _client_socket = None
    try:
        conn.close()
    except OSError:
        pass
    print("[INFO] Client disconnected.")

shutdown = False
while not shutdown:
    print("[INFO] Waiting for client to connect...")
    try:
        conn, addr = server_socket.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()
    except OSError as e:
        print(f"[ERROR] server socket error: {e}")
        break

print("[INFO] Agent server shutting down.")
# ---END OF FILE---
    
