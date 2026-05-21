import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Boxes,
  Cable,
  CheckCircle2,
  Cloud,
  Code2,
  Database,
  FileCode2,
  Gauge,
  KeyRound,
  Layers3,
  LockKeyhole,
  MessageSquarePlus,
  Package,
  Play,
  Radio,
  RefreshCw,
  SearchCode,
  Server,
  ShieldCheck,
  TerminalSquare,
  TimerReset,
  Wrench,
  Workflow,
} from "lucide-react";

export type DocSection = {
  id: string;
  title: string;
  eyebrow?: string;
  description: string;
  icon: LucideIcon;
  code?: string;
  points?: string[];
};

export type NavGroup = {
  title: string;
  items: Array<{ id: string; label: string }>;
};

export const navGroups: NavGroup[] = [
  {
    title: "Getting Started",
    items: [
      { id: "introduction", label: "Introduction" },
      { id: "installation", label: "Installation" },
      { id: "quick-start", label: "Quick Start" },
      { id: "agentfile", label: "AgentFile" },
    ],
  },
  {
    title: "Core Concepts",
    items: [
      { id: "runtime", label: "Runtime" },
      { id: "tools", label: "Tools" },
      { id: "streaming", label: "Streaming" },
      { id: "terminal-mux", label: "Terminal Multiplexer" },
      { id: "sessions", label: "Sessions" },
      { id: "memory", label: "Context Memory" },
      { id: "workspace-change", label: "Workspace Changes" },
    ],
  },
  {
    title: "Production",
    items: [
      { id: "hooks", label: "Hooks" },
      { id: "permissions", label: "Permission Gating" },
      { id: "injection", label: "Mid-task Messages" },
      { id: "custom-tools", label: "Custom Tools" },
      { id: "fastapi", label: "FastAPI Integration" },
      { id: "microvms", label: "MicroVM Architecture" },
      { id: "security", label: "Security Model" },
      { id: "deployment", label: "Deployment" },
    ],
  },
  {
    title: "Reference",
    items: [
      { id: "api", label: "API Reference" },
      { id: "configuration", label: "Configuration" },
      { id: "events", label: "Events" },
      { id: "search", label: "Search Tools" },
      { id: "cli", label: "CLI Pattern" },
      { id: "completion", label: "Completion Block" },
    ],
  },
];

export const sections: DocSection[] = [
  {
    id: "introduction",
    title: "Build agentic coding runtimes without rebuilding the scaffolding.",
    eyebrow: "Introduction",
    description:
      "CodePilot is an embeddable Python library for giving software agents structured access to files, terminals, persistent sessions, and runtime hooks.",
    icon: Code2,
    points: [
      "Embed the runtime in CLIs, FastAPI services, notebooks, desktop apps, or hosted workspaces.",
      "Keep agent behavior observable with stream, tool call, tool result, and finish events.",
      "Bring your own provider, database, sandbox, and product surface.",
    ],
  },
  {
    id: "installation",
    title: "Install the library.",
    eyebrow: "Installation",
    description:
      "Start with the Python package, then add optional dependencies for database persistence or provider-specific workflows.",
    icon: Package,
    code:
      "pip install codepilot-ai\npip install 'codepilot-ai[db]'\n\nexport ANTHROPIC_API_KEY='sk-ant-...'\nexport OPENAI_API_KEY='sk-...'",
  },
  {
    id: "quick-start",
    title: "Run your first task.",
    eyebrow: "Quick Start",
    description:
      "Create an AgentFile, point it at a workspace, and let the runtime coordinate inference, tool calls, and persistence.",
    icon: Play,
    code:
      "from codepilot import Runtime\n\nruntime = Runtime('agent.yaml', stream=True)\nsummary = runtime.run('Fix the nginx config')\nprint(summary)",
  },
  {
    id: "agentfile",
    title: "Every runtime starts from an AgentFile.",
    eyebrow: "AgentFile",
    description:
      "The AgentFile is a YAML contract for model choice, working directory, tool enablement, permission gates, memory settings, and runtime limits. Relative paths resolve from the YAML file location, not the shell CWD.",
    icon: Boxes,
    code:
      "agent:\n  name: CodePilot\n  role: Autonomous software engineering agent.\n  model:\n    provider: anthropic\n    name: claude-sonnet-4-5\n    api_key_env: ANTHROPIC_API_KEY\n  runtime:\n    work_dir: ./workspace\n    max_steps: 20\n    unsafe_mode: false\n  tools:\n    - name: read_file\n      enabled: true\n    - name: write_file\n      enabled: true\n      config:\n        require_permission: false\n    - name: execute\n      enabled: true\n      config:\n        require_permission: true",
  },
  {
    id: "runtime",
    title: "One runtime owns the agent loop.",
    eyebrow: "Runtime",
    description:
      "The runtime turns model responses into a controlled execution pipeline: parse, validate, execute tools, collect results, and continue until completion.",
    icon: Workflow,
    points: [
      "The model receives a refreshed system prompt and the conversation history every step.",
      "Natural language before the control block can stream to the user immediately.",
      "Only the fenced codepilot control block executes; ordinary Python markdown is display-only.",
      "Execution results are fed back to the model as the next user turn.",
      "The loop stops on a completion block, max_steps, or abort.",
    ],
    code:
      "```codepilot\nread_file('routes/profile.py', start_line=35, end_line=65)\n```\n\n```completion\nDone. The task is complete.\n```",
  },
  {
    id: "tools",
    title: "Tools are explicit, observable, and product-friendly.",
    eyebrow: "Tools",
    description:
      "File and terminal tools emit events before and after work so a UI can render exactly what the agent is doing. Built-ins cover file edits, terminal sessions, user questions, text search, and context memory.",
    icon: FileCode2,
    points: [
      "write_file consumes side-loaded payload blocks instead of fragile inline strings.",
      "read_file returns 1-indexed line numbers so edits can target precise ranges.",
      "execute, read_output, send_input, and terminate_terminal work over persistent terminal sessions.",
      "find gives clean ripgrep-backed search results without forcing the model through shell composition.",
    ],
    code:
      "read_file('config.py')\nwrite_file('config.py', mode='edit', start_line=12, end_line=12)\nexecute('main', 'pytest -q', timeout=30)",
  },
  {
    id: "streaming",
    title: "Streaming keeps the interface alive while tools wait.",
    eyebrow: "Streaming",
    description:
      "When stream=True, the assistant's pre-tool explanation streams token by token. The control block and payload blocks buffer silently, then completion text is emitted only after tools physically finish.",
    icon: Radio,
    points: [
      "STREAM hooks receive natural language before the first codepilot fence.",
      "Tool execution remains deterministic because code and payloads are parsed from the complete response.",
      "Completion text is delayed until after file writes and terminal commands have actually run.",
    ],
    code:
      "from codepilot import Runtime, on_stream\n\nruntime = Runtime('agent.yaml', stream=True)\n\n@on_stream(runtime)\ndef stream(text: str):\n    print(text, end='', flush=True)\n\nruntime.run('Refactor the auth middleware')",
  },
  {
    id: "terminal-mux",
    title: "Shared terminal sessions over Unix sockets.",
    eyebrow: "Terminal Multiplexer",
    description:
      "A default terminal session named main starts automatically and persists across run calls. On Linux and macOS, terminal sessions are backed by a PTY multiplexer so the agent and UI clients can attach to the same shell.",
    icon: TerminalSquare,
    points: [
      "execute starts commands and returns completed or running status with return codes when available.",
      "read_output waits for long-running command output without duplicating context.",
      "send_input supports prompts, Ctrl+C, Ctrl+D, arrow keys, and TUI input.",
      "new_terminal=True creates dedicated sessions for servers, REPLs, and long-lived processes.",
    ],
    code:
      "execute('server', 'uvicorn app.main:app --port 8000', timeout=4, new_terminal=True)\nexecute('main', 'pytest tests/test_api.py -v', timeout=30)\nsend_input('server', '\\x03', timeout=5)\nterminate_terminal('server')",
  },
  {
    id: "sessions",
    title: "Persistence is a backend choice.",
    eyebrow: "Sessions",
    description:
      "Choose memory for throwaway tasks, file sessions for local resumes, or SQLAlchemy-backed database sessions for containers and multi-user web applications.",
    icon: Database,
    points: [
      "Memory sessions disappear when the process exits.",
      "File sessions persist JSON under ~/.codepilot/sessions unless a custom directory is supplied.",
      "Database sessions create the codepilot_sessions table automatically.",
      "AsyncDatabaseSession reuses a caller-supplied SQLAlchemy AsyncEngine.",
    ],
    code:
      "runtime = Runtime('agent.yaml', session='memory')\nruntime = Runtime('agent.yaml', session='file', session_id='ecommerce-api')\nruntime = Runtime('agent.yaml', session='db', db_url='sqlite:///./codepilot.db')\n\nengine = create_async_engine(DATABASE_URL, pool_size=1, max_overflow=2)\nruntime = AsyncRuntime('agent.yaml', session='db', db=engine)",
  },
  {
    id: "memory",
    title: "Long sessions can archive context instead of drowning in it.",
    eyebrow: "Context Memory",
    description:
      "The memory manager tracks task boundaries and lets agents archive completed task context with summaries. Archived tasks stay visible as compact summaries and can be revealed later.",
    icon: TimerReset,
    points: [
      "archive_context stores a summary for one or more completed tasks.",
      "reveal_context restores the full original content of an archived task.",
      "list_archived_context shows summaries and estimated token savings.",
    ],
    code:
      "archive_context(position=2, summary='Added FastAPI routes and SQLAlchemy models.')\nreveal_context(2)\nlist_archived_context()",
  },
  {
    id: "workspace-change",
    title: "The runtime notices when humans edit watched files.",
    eyebrow: "Workspace Change Detection",
    description:
      "Between agent steps, CodePilot compares snapshots for files the agent has touched. If a human or external tool changes the workspace, the next prompt receives a precise environment-change message.",
    icon: RefreshCw,
    points: [
      "Only files the agent has read or written are watched.",
      "There is no background daemon and no filesystem watcher overhead.",
      "Changed line ranges are reported so the agent knows to re-read before editing.",
      "Created and deleted files are included in the environment notification.",
    ],
    code:
      "[ENVIRONMENT CHANGE] 2026-02-21 16:30:12\n\nModified: main.py\nChanged lines: 1-4, 47\nCreated: .env (3 lines)\nDeleted: old_config.py",
  },
  {
    id: "hooks",
    title: "Hooks are the product integration layer.",
    eyebrow: "Hooks",
    description:
      "Every important runtime event passes through HookSystem. Apps use these events to stream text, render tool activity, request permission, and close tasks cleanly.",
    icon: Activity,
    points: [
      "START and STEP indicate task lifecycle progress.",
      "STREAM powers token-level UI updates.",
      "TOOL_CALL and TOOL_RESULT power activity timelines.",
      "ASK_USER and PERMISSION_REQUEST let the runtime pause for human input.",
      "FINISH and MAX_STEPS let clients restore UI state.",
    ],
    code:
      "from codepilot import on_tool_call, on_tool_result, on_finish\n\n@on_tool_call(runtime)\ndef tool_call(tool: str, args: dict, label: str = ''):\n    send_to_ui({'event': 'tool_call', 'tool': tool, 'args': args, 'label': label})\n\n@on_tool_result(runtime)\ndef tool_result(tool: str, result: str):\n    send_to_ui({'event': 'tool_result', 'tool': tool, 'result': result})",
  },
  {
    id: "permissions",
    title: "Risky operations can require approval.",
    eyebrow: "Permission Gating",
    description:
      "The execute tool and optionally write_file can ask for permission before running. If no handler is registered, CodePilot falls back to a CLI yes/no prompt.",
    icon: CheckCircle2,
    points: [
      "Enable require_permission in the AgentFile for execute or write_file.",
      "Return True to approve and False to deny.",
      "Use this for local tools, hosted demos, or enterprise approval workflows.",
    ],
    code:
      "from codepilot import on_permission_request\n\n@on_permission_request(runtime)\ndef approve(tool: str, description: str) -> bool:\n    if tool == 'execute' and 'pytest' in description:\n        return True\n    return False",
  },
  {
    id: "injection",
    title: "User messages can join the task at safe boundaries.",
    eyebrow: "Mid-task Messages",
    description:
      "Applications can inject user feedback while a task is running. Messages are queued immediately, then inserted at the next agentic step boundary so tools are never interrupted mid-side-effect.",
    icon: MessageSquarePlus,
    points: [
      "send_message is thread-safe and non-blocking.",
      "Injected messages are tagged separately from the original task.",
      "The current control block is allowed to finish before new instructions enter context.",
    ],
    code:
      "from codepilot import AsyncRuntime, on_user_message_injected\n\nruntime = AsyncRuntime('agent.yaml', stream=True)\n\n@on_user_message_injected(runtime)\ndef confirmed(message: str, **_):\n    print(f'User update is now in context: {message}')\n\n# From another thread, websocket handler, or UI callback:\nruntime.send_message('Prefer SQLAlchemy async sessions for the repository layer.')",
  },
  {
    id: "custom-tools",
    title: "Applications can register their own tools.",
    eyebrow: "Custom Tools",
    description:
      "Any callable can become a tool. Its docstring is injected into the system prompt so the agent learns when to use it. If the tool produces output the model should see, append that output to the execution buffer.",
    icon: Wrench,
    points: [
      "Use register_tool for domain-specific APIs such as search, Slack, deployments, or internal services.",
      "Use replace=True to override a built-in tool with a safer policy wrapper.",
      "Custom tools are part of the same observable tool-call lifecycle as built-ins.",
    ],
    code:
      "from codepilot import Runtime\n\nruntime = Runtime('agent.yaml')\n\ndef send_slack(channel: str, message: str):\n    \"\"\"\n    Send a message to a Slack channel after completing a task.\n    channel should be the name without #, for example 'deployments'.\n    \"\"\"\n    slack_client.chat_postMessage(channel=f'#{channel}', text=message)\n    runtime._async._append_execution(f'[send_slack] Message sent to #{channel}.')\n\nruntime.register_tool('send_slack', send_slack)\nruntime.run('Fix the deployment script and notify Slack when done.')",
  },
  {
    id: "fastapi",
    title: "FastAPI can own the control plane.",
    eyebrow: "FastAPI Integration",
    description:
      "Expose tasks, events, auth, and workspace lifecycle through your web application while CodePilot handles the agent runtime. Use AsyncRuntime directly in async applications.",
    icon: Cable,
    points: [
      "Async-first for web applications and long-lived services.",
      "Use WebSockets or Server-Sent Events for runtime event streaming.",
      "Bridge hook callbacks into an async queue for browser clients.",
      "Keep one SQLAlchemy engine per process and pass it into the runtime.",
    ],
    code:
      "import asyncio\nfrom fastapi import FastAPI, WebSocket\nfrom codepilot import AsyncRuntime, EventType\n\napp = FastAPI()\nruntime = AsyncRuntime('agent.yaml', session='db', db=engine, stream=True)\nevents: asyncio.Queue[dict] = asyncio.Queue()\n\nruntime.hooks.register(EventType.STREAM, lambda text, **_: events.put_nowait({'type': 'stream', 'text': text}))\nruntime.hooks.register(EventType.TOOL_CALL, lambda tool, args, label='', **_: events.put_nowait({'type': 'tool_call', 'tool': tool, 'label': label}))\nruntime.hooks.register(EventType.FINISH, lambda summary, **_: events.put_nowait({'type': 'finish', 'summary': summary}))\n\n@app.post('/task')\nasync def task(payload: dict):\n    asyncio.create_task(runtime.run(payload['task']))\n    return {'status': 'started'}\n\n@app.websocket('/events')\nasync def stream(ws: WebSocket):\n    await ws.accept()\n    while True:\n        await ws.send_json(await events.get())",
  },
  {
    id: "microvms",
    title: "Disposable sandboxes make hosted agents safer.",
    eyebrow: "MicroVM Architecture",
    description:
      "Run code-server and CodePilot inside a per-user machine, persist structured state to Postgres, and sync workspace artifacts to object storage.",
    icon: Cloud,
    points: [
      "Fly Machines or similar runtimes provide isolated execution planes.",
      "The browser talks to code-server; the extension talks to CodePilot over local IPC.",
      "Destroy idle machines after syncing durable state.",
    ],
    code:
      "Browser -> Fly proxy -> code-server :8080\ncode-server extension -> /run/codepilot/runtime.sock\ncodepilot daemon -> /tmp/codepilot_main.sock\ncodepilot daemon -> Postgres + object storage + workspace files",
  },
  {
    id: "security",
    title: "Treat the runtime as powerful infrastructure.",
    eyebrow: "Security Model",
    description:
      "CodePilot intentionally gives agents real coding capabilities. Product deployments should pair it with external sandboxing and approval policies.",
    icon: ShieldCheck,
    points: [
      "Use containers, MicroVMs, or OS sandboxing around untrusted workspaces.",
      "Gate risky tools with permission hooks.",
      "Separate user auth, machine auth, and database credentials.",
    ],
  },
  {
    id: "deployment",
    title: "Ship the library, demo the product surface.",
    eyebrow: "Deployment",
    description:
      "The package stays reusable, while the hosted documentation and demo workspace prove the system works end to end.",
    icon: Gauge,
    points: [
      "Docs deploy as a static GitHub Pages site.",
      "The hosted workspace can run code-server plus a runtime daemon.",
      "The same runtime can later power VS Code, desktop, or API experiences.",
    ],
  },
  {
    id: "api",
    title: "The public API is intentionally small.",
    eyebrow: "API Reference",
    description:
      "Most applications only need Runtime or AsyncRuntime, hook decorators, session backends, and the built-in tools the agent calls from control blocks.",
    icon: Layers3,
    points: [
      "Runtime is the synchronous wrapper for CLI scripts and simple integrations.",
      "AsyncRuntime is the preferred surface for FastAPI and long-lived async services.",
      "register_tool lets applications add domain-specific functions to the agent sandbox.",
      "send_message injects mid-task user input without stopping the current step.",
      "abort stops after the current step, preserving tool side-effect semantics.",
    ],
    code:
      "Runtime(agent_file, session='memory', session_id=None, stream=False, db_url=None, db=None)\nAsyncRuntime(agent_file, session='memory', session_id=None, stream=False, db_url=None, db=None)\n\nruntime.run(task: str)\nruntime.send_message(message: str)\nruntime.abort()\nruntime.reset()\nruntime.register_tool(name, func, replace=False)",
  },
  {
    id: "configuration",
    title: "Configuration should be readable at a glance.",
    eyebrow: "Configuration",
    description:
      "AgentFiles describe the model, runtime, tools, memory, and workspace contract. The docs structure leaves room for every field.",
    icon: Boxes,
    code:
      "agent:\n  name: CodePilot\n  model:\n    provider: openai\n    name: gpt-4.1\n  runtime:\n    work_dir: ./workspace\n    max_steps: 8",
  },
  {
    id: "events",
    title: "Events are the UI contract.",
    eyebrow: "Events",
    description:
      "Runtime hooks let products stream progress, render tool activity, request approvals, and show task completion without coupling UI code to internals.",
    icon: Activity,
    points: [
      "STREAM renders assistant progress.",
      "TOOL_CALL and TOOL_RESULT drive activity timelines.",
      "FINISH closes the task with a summary.",
    ],
    code:
      "START(task)\nSTEP(step, max_steps)\nSTREAM(text)\nTOOL_CALL(tool, args, label)\nTOOL_RESULT(tool, result)\nASK_USER(question)\nPERMISSION_REQUEST(tool, description)\nRUNTIME_ERROR(error)\nFINISH(summary)\nMAX_STEPS()",
  },
  {
    id: "search",
    title: "Use text search first, semantic search when names are unknown.",
    eyebrow: "Search Tools",
    description:
      "CodePilot includes a fast text/regex search tool and optional semantic search through grepai. This lets the agent inspect code by exact symbol or by concept.",
    icon: SearchCode,
    points: [
      "find uses ripgrep when available and falls back to a Python implementation.",
      "Use find when the agent knows the symbol, import, class name, or literal text.",
      "Use semantic_search when the agent only knows the concept or dependency relationship.",
      "grepai indexes outside the project under ~/.codepilot/grepai so it does not pollute repositories.",
    ],
    code:
      "find(pattern=r'validate_email\\(', scope='file', target='routes/profile.py')\nfind(pattern='TODO:', scope='files', target=['routes/profile.py', 'utils/validators.py'])\nfind(pattern=r'class \\w+Handler', scope='codebase', include='*.py')\n\nsemantic_search('where is user session persistence implemented?', mode='search', top_k=5)\nsemantic_search('trace callers of validate_email', mode='trace_callers', depth=2)",
  },
  {
    id: "cli",
    title: "A minimal CLI is only a thin product shell.",
    eyebrow: "CLI Pattern",
    description:
      "Because CodePilot is library-first, a local CLI or internal tool mostly wires hooks to stdout, chooses a session backend, and forwards user input into runtime.run.",
    icon: Server,
    points: [
      "Use memory sessions for throwaway conversations.",
      "Use file sessions when users should resume by session id.",
      "Use hooks to customize display without changing runtime internals.",
    ],
    code:
      "from codepilot import Runtime, on_stream, on_finish\n\nruntime = Runtime('agent.yaml', session='file', session_id='default', stream=True)\n\n@on_stream(runtime)\ndef stream(text: str, **_):\n    print(text, end='', flush=True)\n\n@on_finish(runtime)\ndef finish(summary: str, **_):\n    print(f'\\nDone: {summary}\\n')\n\nwhile True:\n    task = input('You: ').strip()\n    if task in {'quit', 'exit'}:\n        break\n    if task == 'reset':\n        runtime.reset()\n        continue\n    runtime.run(task)",
  },
  {
    id: "completion",
    title: "Completion is explicit, not guessed.",
    eyebrow: "Completion Block",
    description:
      "The completion block is the agent's deliberate signal that the task is done. This avoids guessing from natural language and gives applications a clean final summary.",
    icon: CheckCircle2,
    points: [
      "A completion block may appear in the same step as a simple tool action.",
      "For terminal commands, the agent should wait for execution results before completing.",
      "run() returns the completion text, or None if max_steps or abort stops the loop.",
    ],
    code:
      "```completion\nDone. Updated TIMEOUT to 30 seconds and verified the tests pass.\n```",
  },
];

export const stats = [
  { label: "Install", value: "pip" },
  { label: "Runtime", value: "Async-first" },
  { label: "Storage", value: "SQLAlchemy" },
  { label: "IPC", value: "Unix socket" },
];

export const featureCards = [
  {
    title: "Embeddable",
    description: "Bring CodePilot into your own product surface instead of forcing users into a single app.",
    icon: KeyRound,
  },
  {
    title: "Observable",
    description: "Every stream, tool call, terminal result, and finish event can be rendered in your UI.",
    icon: Activity,
  },
  {
    title: "Sandbox-ready",
    description: "Designed to live inside containers, MicroVMs, and hosted coding environments.",
    icon: LockKeyhole,
  },
];
