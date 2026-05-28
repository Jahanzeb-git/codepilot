import { Code, Callout, Section, PageHeader } from "./components";

export function PageCustomTools() {
  return (
    <>
      <PageHeader
        title="Custom Tools"
        subtitle="Register any callable as a tool. Its docstring is automatically pulled into the system prompt so the agent knows when and how to use it."
      />

      <Callout>
        <strong>Important:</strong> custom tools are invoked from the agent control block. If your tool produces
        output the agent should see, explicitly append it to the execution buffer by calling{" "}
        <code>runtime._async._append_execution(...)</code> when using <code>Runtime</code>.
      </Callout>

      <Section title="Adding custom tools">
        <Code lang="python">{`from codepilot import Runtime

runtime = Runtime("agent.yaml")


def web_search(query: str):
    """
    Search the web for current information and return a summary.
    Use for library documentation, recent API changes, error lookups,
    or anything the codebase snapshot can't answer.
    """
    result = my_search_api(query)
    runtime._async._append_execution(f"[web_search] {result}")


def send_slack(channel: str, message: str):
    """
    Send a message to a Slack channel.
    Use after completing a task to notify the team.
    channel should be the channel name without #, e.g. 'deployments'.
    """
    slack_client.chat_postMessage(channel=f"#{channel}", text=message)
    runtime._async._append_execution(f"[send_slack] Message sent to #{channel}.")


runtime.register_tool("web_search", web_search)
runtime.register_tool("send_slack", send_slack)

runtime.run("Research the latest SQLAlchemy 2.0 async API and implement a connection pool")`}</Code>
      </Section>

      <Section title="Overriding a built-in tool">
        <Code lang="python">{`def safe_execute(session_id: str, command: str, timeout: int = 10, new_terminal: bool = False, shell=None):
    """
    Run a shell command. Restricted to read-only operations in this environment.
    Never import subprocess or os directly; always use this tool.
    """
    blocked = ["rm", "del", "format", ">", "sudo", "pip install"]
    if any(cmd in command for cmd in blocked):
        runtime._async._append_execution(f"[execute] Blocked: '{command}' is not permitted.")
        return
    return runtime._async._terminal_manager.execute(session_id, command, timeout, new_terminal, shell)


runtime.register_tool("execute", safe_execute, replace=True)`}</Code>
      </Section>
    </>
  );
}

export function PageAborting() {
  return (
    <>
      <PageHeader
        title="Aborting the Agent"
        subtitle="Stop the agent after the current step completes — never mid-step."
      />

      <Section>
        <Code lang="python">{`import asyncio
from codepilot import AsyncRuntime

runtime = AsyncRuntime("agent.yaml")

agent_task = asyncio.create_task(
    runtime.run("Build a complete e-commerce backend")
)

# From anywhere — stops after the current step completes (never mid-step)
runtime.abort()
await agent_task`}</Code>
      </Section>
    </>
  );
}

export function PageCLIPattern() {
  return (
    <>
      <PageHeader
        title="Building a CLI Tool"
        subtitle="Because CodePilot is library-first, a local CLI mostly wires hooks to stdout, chooses a session backend, and forwards user input into runtime.run()."
      />

      <Section title="Simple conversational CLI">
        <Code lang="python">{`import sys
from codepilot import Runtime, on_stream, on_finish, on_ask_user

runtime = Runtime("agent.yaml", session="memory", stream=True)


@on_stream(runtime)
def show_stream(text: str, **_):
    print(text, end="", flush=True)


@on_finish(runtime)
def show_done(summary: str, **_):
    print(f"\\n{summary}\\n")


@on_ask_user(runtime)
def show_question(question: str, **_):
    print(f"\\n{question}")


print("CodePilot CLI — type 'reset' to clear history, 'quit' to exit.\\n")

while True:
    try:
        task = input("You: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\\nGoodbye.")
        sys.exit(0)

    if not task:
        continue
    if task.lower() == "quit":
        sys.exit(0)
    if task.lower() == "reset":
        runtime.reset()
        print("History cleared. Starting fresh.\\n")
        continue

    runtime.run(task)`}</Code>
      </Section>

      <Section title="File-backed CLI with named sessions">
        <Code lang="python">{`import sys
import argparse
from codepilot import Runtime, FileSession, on_stream, on_finish

parser = argparse.ArgumentParser()
parser.add_argument("--session", default=None, help="Session ID to resume")
parser.add_argument("--list", action="store_true", help="List saved sessions")
args = parser.parse_args()

if args.list:
    fs = FileSession(session_id="_", agent_name="_")
    sessions = fs.list_sessions()
    if not sessions:
        print("No saved sessions.")
    for s in sessions:
        print(f"  {s['session_id']:30} {s['messages']:4} messages")
    sys.exit(0)

session_id = args.session or "default"
runtime = Runtime("agent.yaml", session="file", session_id=session_id, stream=True)

fs = FileSession(session_id=session_id, agent_name="")
if fs.exists():
    print(f"Resuming session '{session_id}' ({len(runtime.messages)} messages)\\n")
else:
    print(f"Starting new session '{session_id}'\\n")


@on_stream(runtime)
def streaming(text: str, **_):
    print(text, end="", flush=True)


@on_finish(runtime)
def done(summary: str, **_):
    print(f"\\nDone: {summary}\\n")


while True:
    try:
        task = input("You: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\\nSession saved. Goodbye.")
        sys.exit(0)

    if not task:
        continue
    if task.lower() in ("reset", "clear"):
        runtime.reset()
        print("Session cleared.\\n")
        continue
    if task.lower() in ("quit", "exit"):
        sys.exit(0)

    runtime.run(task)`}</Code>
        <Code lang="bash">{`python cli.py                              # new default session
python cli.py --session ecommerce-api      # resume named session
python cli.py --list                       # show all saved sessions`}</Code>
      </Section>
    </>
  );
}

export function PageWebServer() {
  return (
    <>
      <PageHeader
        title="Building a Web Server Integration"
        subtitle="FastAPI example with WebSocket streaming (token-by-token to the browser) and mid-task injection."
      />

      <Section>
        <Code lang="python">{`import asyncio
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from codepilot import Runtime, EventType

app = FastAPI()

runtime = Runtime("agent.yaml", session="file", session_id="web-session", stream=True)

# Bridge between sync hooks and async WebSocket
_event_queue: asyncio.Queue = asyncio.Queue()


def _push(event: dict):
    """Thread-safe push from sync hook into async queue."""
    asyncio.get_event_loop().call_soon_threadsafe(_event_queue.put_nowait, event)


# Stream reasoning text and completion block content token by token
runtime.hooks.register(EventType.STREAM,
    lambda text, **_: _push({"type": "stream", "text": text}))

# Tool activity label gives a clean human-readable status string
runtime.hooks.register(EventType.TOOL_CALL,
    lambda tool, args, label="", **_: _push({
        "type": "tool_call", "tool": tool,
        "label": label or tool,           # e.g. "Running pytest tests/"
    }))

runtime.hooks.register(EventType.TOOL_RESULT,
    lambda tool, result, **_: _push({"type": "tool_result", "tool": tool, "result": result[:300]}))

runtime.hooks.register(EventType.FINISH,
    lambda summary, **_: _push({"type": "finish", "summary": summary}))

runtime.hooks.register(EventType.RUNTIME_ERROR,
    lambda error, **_: _push({"type": "error", "error": error}))


@app.post("/run")
def start_task(task: str):
    """Start a new task. Non-blocking; the agent runs in a background thread."""
    threading.Thread(target=runtime.run, args=(task,), daemon=True).start()
    return {"status": "started"}


@app.post("/message")
def inject_message(message: str):
    """Inject a mid-task message. Returns immediately."""
    runtime.send_message(message)
    return {"status": "queued"}


@app.post("/reset")
def reset_session():
    """Wipe conversation history and start fresh."""
    runtime.reset()
    return {"status": "reset"}


@app.websocket("/events")
async def stream_events(websocket: WebSocket):
    """Stream all hook events to the frontend as JSON."""
    await websocket.accept()
    try:
        while True:
            event = await _event_queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass`}</Code>
      </Section>
    </>
  );
}
