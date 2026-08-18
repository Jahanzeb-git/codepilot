import { Code, Callout, Table, Section, PageHeader } from "./components";

export function PageHooks() {
  return (
    <>
      <PageHeader
        title="Hooks"
        subtitle="Hooks are the observability system. Every significant runtime event fires a hook. Register handlers to receive them in your application."
      />

      <Callout>
        All built-in decorators replace the default stdout handler. The defaults work out of the box with zero configuration.
      </Callout>

      <Section title="Full hooks example">
        <Code lang="python">{`from codepilot import (
    Runtime,
    on_stream,
    on_tool_call,
    on_tool_result,
    on_ask_user,
    on_finish,
    on_user_message_queued,
    on_user_message_injected,
    EventType,
)

runtime = Runtime("agent.yaml", stream=True)


@on_stream(runtime)
def handle_stream(text: str, **_):
    """Fires for each text chunk of natural-language agent output."""
    print(text, end="", flush=True)


@on_tool_call(runtime)
def handle_tool_call(tool: str, args: dict, label: str = "", **_):
    """Fires before every tool executes.
    label is a human-readable description (e.g. "Running pytest tests/").
    Falls back to args dump if label is not set.
    """
    display = label if label else str(args)
    print(f"\\n[tool:{tool}] {display}")


@on_tool_result(runtime)
def handle_tool_result(tool: str, result: str, **_):
    """Fires after every tool returns."""
    print(f"{result[:200]}")


@on_ask_user(runtime)
def handle_ask(question: str, **_):
    """Fires when the agent calls ask_user()."""
    print(f"\\n{question}")


@on_finish(runtime)
def handle_finish(summary: str, **_):
    """Fires when the agent completes the task with task(finish=True)."""
    print(f"\\n{summary}\\n")


@on_user_message_queued(runtime)
def handle_queued(message: str, **_):
    """Fires immediately when send_message() is called (not yet in context)."""
    print(f"[Queued] {message}")


@on_user_message_injected(runtime)
def handle_injected(message: str, **_):
    """Fires when a queued message enters the LLM's context window."""
    print(f"[Injected] {message}")


runtime.run("Refactor the database module to use async SQLAlchemy")`}</Code>
      </Section>

      <Section title="Manual hook registration">
        <Code lang="python">{`from codepilot import EventType

runtime.hooks.register(EventType.STREAM,  lambda text, **_: print(text, end="", flush=True))
runtime.hooks.register(EventType.FINISH,  lambda summary, **_: save_to_db(summary))`}</Code>
      </Section>

      <Section title="Full event reference">
        <Table
          headers={["Event", "Keyword args", "When it fires"]}
          rows={[
            [<code>START</code>, <code>task</code>, "run() is called"],
            [<code>STEP</code>, <code>step, max_steps</code>, "Each agentic step begins"],
            [<code>STREAM</code>, <code>text</code>, "Chunk of natural-language agent output"],
            [<code>TOOL_CALL</code>, <code>tool, args, label</code>, "Before any tool executes"],
            [<code>TOOL_RESULT</code>, <code>tool, result</code>, "After any tool returns"],
            [<code>ASK_USER</code>, <code>question</code>, "Agent calls ask_user()"],
            [<code>PERMISSION_REQUEST</code>, <code>tool, description</code>, "Tool with require_permission: true fires"],
            [<code>SECURITY_ERROR</code>, <code>error</code>, "AST validation rejects the control block"],
            [<code>RUNTIME_ERROR</code>, <code>error</code>, "Provider, parser, or control-block execution error occurs"],
            [<code>FINISH</code>, <code>summary</code>, "Task complete — task(finish=True) called"],
            [<code>MAX_STEPS</code>, "—", "Loop exits because max_steps was reached"],
            [<code>USER_MESSAGE_QUEUED</code>, <code>message</code>, "send_message() called"],
            [<code>USER_MESSAGE_INJECTED</code>, <code>message</code>, "Queued message enters LLM context"],
            [<code>SESSION_RESET</code>, "—", "reset() called"],
          ]}
        />
      </Section>
    </>
  );
}

export function PagePermissionGating() {
  return (
    <>
      <PageHeader
        title="Permission Gating"
        subtitle="The execute tool (and optionally file_editor) supports require_permission: true in the AgentFile. A PERMISSION_REQUEST hook fires before the tool runs."
      />

      <Section>
        <p>Return <code>True</code> to approve, <code>False</code> to deny. Falls back to a CLI y/N prompt if no handler is registered.</p>
        <Code lang="python">{`from codepilot import Runtime, on_permission_request

runtime = Runtime("agent.yaml")


@on_permission_request(runtime)
def gate(tool: str, description: str, **_) -> bool:
    """
    tool: "file_editor" | "execute"
    description: human-readable description of the specific operation
    Return True to approve, False to deny.
    """
    print(f"\\n[{tool}] {description}")
    return input("Approve? [y/N]: ").strip().lower() in ("y", "yes")


runtime.run("Deploy the application")`}</Code>
      </Section>

      <Section title="Programmatic approval (e.g. in a web app)">
        <Code lang="python">{`@on_permission_request(runtime)
def auto_gate(tool: str, description: str, **_) -> bool:
    if tool == "file_editor" and "config.py" in description:
        return True
    if tool == "execute" and "pytest" in description:
        return True
    return False   # deny everything else`}</Code>
      </Section>
    </>
  );
}

export function PageMidTaskInjection() {
  return (
    <>
      <PageHeader
        title="Mid-task Message Injection"
        subtitle="From any other thread, call runtime.send_message() to inject a message into the running agent."
      />

      <Section title="How it works">
        <ol style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li>Queued immediately (non-blocking, thread-safe)</li>
          <li>Tagged <code>[USER MESSAGE]</code> and kept distinct from <code>[USER INPUT]</code> (the original task)</li>
          <li>Injected into the LLM context at the <strong>next step boundary</strong> — never mid-step</li>
        </ol>
      </Section>

      <Section title="Example">
        <Code lang="python">{`import time
from codepilot import AsyncRuntime, on_stream, on_user_message_injected

runtime = AsyncRuntime("agent.yaml", stream=True)


@on_stream(runtime)
def show(text: str, **_):
    print(text, end="", flush=True)


@on_user_message_injected(runtime)
def confirmed(message: str, **_):
    print(f"\\n[Your message is now in context]: {message}")


async def run_agent():
    await runtime.run("Create a utility module with five string helper functions")`}</Code>
      </Section>
    </>
  );
}

export function PageMultiOperation() {
  return (
    <>
      <PageHeader
        title="Multi-operation Steps"
        subtitle="The agent can perform multiple file operations in a single step, reducing round-trips and improving efficiency."
      />

      <Section title="Multiple file writes">
        <p>
          Up to <strong>5 <code>file_editor()</code> calls</strong> with <code>mode='create'</code> or{" "}
          <code>mode='a'</code> per step. Each call consumes the next payload block in order.
        </p>
        <Code lang="text">{`Alright, both files are independent so I'll write them together.

\`\`\`codepilot
# Two new files — order of file_editor() matches order of payload blocks below.
file_editor("config.py", mode="create")
file_editor("utils.py", mode="create")
\`\`\`

\`\`\`python filename=config.py
import json, os

def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)
\`\`\`

\`\`\`python filename=utils.py
def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")
\`\`\``}</Code>
      </Section>

      <Section title="Multi-edit (multiple non-contiguous edits in one file)">
        <p>
          Use <code>mode='multi_edit'</code> with <code>edits=[(start1, end1), (start2, end2)]</code> to fix
          multiple ranges in one file without line-number drift. The runtime applies edits bottom-to-top
          automatically. One payload block per tuple, in order.
        </p>
        <Code lang="text">{`\`\`\`codepilot
# Fix L42-48 (error handling) and L55 (regex) in one step — no drift
file_editor("routes/profile.py", mode="edit")
\`\`\`

\`\`\`python filename=routes/profile.py
# ... replacement for L42-48 ...
\`\`\`

\`\`\`python filename=routes/profile.py
# ... replacement for L55 ...
\`\`\``}</Code>
      </Section>

      <Section title="Multiple file reads">
        <p>Any number of <code>read_file()</code> calls per step — no limit.</p>
        <Code lang="python">{`# LLM control block:
read_file("config.py")
read_file("utils.py")
read_file("tests/test_config.py")`}</Code>
      </Section>
    </>
  );
}
