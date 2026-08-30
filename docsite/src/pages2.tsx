import { Code, Callout, Table, Section, PageHeader } from "./components";

export function PageHowItWorks() {
  return (
    <>
      <PageHeader
        title="How It Works"
        subtitle="CodePilot uses a code-as-interface paradigm. Instead of the LLM describing actions in JSON, it writes Python code that the runtime executes directly."
      />

      <Section title="Each agent step">
        <ol style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li>LLM receives the system prompt (refreshed every step) + full conversation history</li>
          <li>LLM writes a natural language reasoning paragraph (streamed to user in real time), then one or more <code>&lt;path&gt;</code> / SEARCH-REPLACE conflict-marker blocks</li>
          <li>Runtime applies each file block against the current file content and executes the ephemeral <code>codepilot.py</code> block in a sandboxed environment with bound tool functions</li>
          <li>Execution result is appended to conversation history as <code>[EXECUTION RESULT]</code></li>
          <li>Repeat until the agent calls <code>task(finish=True)</code> inside <code>codepilot.py</code>, hits <code>max_steps</code>, or is aborted</li>
        </ol>
      </Section>

      <Section title="The block format">
        <Table
          headers={["Block", "Syntax", "Purpose"]}
          rows={[
            [<strong>File Block</strong>, <code>{"<path>"}</code> + <code>{"<<<<<<< SEARCH ... >>>>>>> REPLACE"}</code>, "Mutates or creates the named workspace file. Empty SEARCH means full overwrite/create."],
            [<strong>codepilot.py Block</strong>, "Same conflict-marker syntax, path is always codepilot.py", "The ephemeral, executable action script. Always uses an empty SEARCH section — one per step."],
          ]}
        />
      </Section>

      <Section title="Action step (more work needed)">
        <Code lang="text">{`Alright, let me read the file first to get the line numbers.

codepilot.py
<<<<<<< SEARCH
=======
view_file("routes/profile.py", start_line=35, end_line=65)
>>>>>>> REPLACE`}</Code>
      </Section>

      <Section title="Single-step task (action + finish)">
        <Code lang="text">{`Got it, updating the timeout value.

config.py
<<<<<<< SEARCH
TIMEOUT = 30
=======
TIMEOUT = 60
>>>>>>> REPLACE

codepilot.py
<<<<<<< SEARCH
=======
task(finish=True)
>>>>>>> REPLACE

Done. Updated TIMEOUT to 60s in config.py.`}</Code>
      </Section>

      <Section title="Chat/explanation (no execution)">
        <Code lang="text">{`Sure! Here's how the config loader handles missing files:

\`\`\`python
# Display block — never executed
def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}   # returns empty dict as default
    with open(path) as f:
        return json.load(f)
\`\`\`

The fallback is an empty dict, so callers always get a valid dict — no None checks needed.`}</Code>
      </Section>
    </>
  );
}

export function PageBasicUsage() {
  return (
    <>
      <PageHeader
        title="Basic Usage"
        subtitle="Run the agent synchronously or asynchronously."
      />

      <Section title="Sync Usage">
        <Code lang="python">{`from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Fix the nginx config")
print(summary)  # final natural-language text, or None`}</Code>
      </Section>

      <Section title="Async Usage">
        <Code lang="python">{`import asyncio
from codepilot import AsyncRuntime

runtime = AsyncRuntime("agent.yaml")

async def main():
    summary = await runtime.run("Fix the nginx config")
    print(summary)

if __name__ == "__main__":
    asyncio.run(main())`}</Code>
      </Section>

      <Callout>
        <code>run()</code> returns when the agent calls <code>task(finish=True)</code>, hits <code>max_steps</code>, or is
        aborted. The return value is its final natural-language text, or <code>None</code> if it ended another way.
      </Callout>
    </>
  );
}

export function PageStreaming() {
  return (
    <>
      <PageHeader
        title="Streaming"
        subtitle="Enable streaming to receive the agent's reasoning text token-by-token, in real time, before any code executes. This dramatically improves perceived responsiveness."
      />

      <Section title="Basic streaming">
        <Code lang="python">{`from codepilot import Runtime, on_stream

runtime = Runtime("agent.yaml", stream=True)


@on_stream(runtime)
def handle_stream(text: str, **_):
    """Fires with each chunk of streamed text."""
    print(text, end="", flush=True)


runtime.run("Diagnose the CI pipeline for the latest failure and stage the fix.")`}</Code>
      </Section>

      <Section title="What gets streamed">
        <p>The runtime streams natural-language text around the execution protocol:</p>
        <ol style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li><strong>Pre-control text</strong> — everything before the <code>{"`"}codepilot{"`"}</code> block streams as the LLM generates it.</li>
          <li><strong>Final text</strong> — after a successful <code>task(finish=True)</code>, trailing natural-language text is emitted after tools finish.</li>
        </ol>
        <p style={{ marginTop: 12 }}>
          Everything between the two windows (the codepilot block, payload blocks) is buffered silently while tools execute.
        </p>
        <p>
          For <strong>chat/question responses</strong> (no <code>codepilot</code> block at all), the entire response streams token-by-token and the loop exits cleanly.
        </p>
      </Section>

      <Section title="Non-streaming mode">
        <p>
          Without <code>stream=True</code>, the full response is emitted as a single <code>STREAM</code> event when inference completes.
          The <code>on_stream</code> hook still fires — you see the complete text at once rather than token-by-token.
        </p>
        <Code lang="python">{`runtime = Runtime("agent.yaml")   # stream=False by default

@on_stream(runtime)
def show_reasoning(text: str, **_):
    print(f"\\n{text}\\n")`}</Code>
      </Section>
    </>
  );
}

export function PageMultiTurn() {
  return (
    <>
      <PageHeader
        title="Multi-turn Execution"
        subtitle="Call run() multiple times on the same Runtime instance. Each call appends to the shared conversation history."
      />

      <Section>
        <p>
          The LLM sees every prior task, every file it wrote, and every command it ran.
        </p>
        <Code lang="python">{`from codepilot import Runtime

runtime = Runtime("agent.yaml")

# Turn 1
runtime.run("Create a FastAPI app with a /items GET endpoint")

# Turn 2 — agent has full context of what it built in turn 1
runtime.run("Now add a POST /items endpoint with Pydantic validation")

# Turn 3 — agent knows the full codebase it has built
runtime.run("Add pytest tests for both endpoints")`}</Code>
      </Section>
    </>
  );
}

export function PageCodeAsInterface() {
  return (
    <>
      <PageHeader
        title="Code-as-Interface"
        subtitle="Understand the philosophy behind treating execution blocks as the primary interface between the model and the environment."
      />

      <Section title="Philosophy & Cognitive Span">
        <p>
          Traditional agent systems constrain models inside narrow, structured JSON schemas or rigid function-calling definitions.
          CodePilot reverses this: the model is given a <strong>code-as-interface</strong> runtime.
          The model writes arbitrary Python code that executes directly in the workspace environment.
        </p>
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li><strong>Large Cognitive Span:</strong> Rather than executing single tools sequentially, the model can reason, write logic loops, import standard libraries, handle exceptions with try-except, and perform complex scripting operations in a single step.</li>
          <li><strong>Flexibility:</strong> The agent can combine built-in tool calls with raw, custom Python code to diagnose problems or inspect outputs dynamically.</li>
        </ul>
      </Section>

      <Section title="The Escaping Problem in JSON APIs">
        <p>
          A common failure point in structured tool calling (JSON / functions) is writing file contents.
          When a tool accepts file content as a string argument within a JSON structure, the model must escape quotes, backslashes, and control characters:
        </p>
        <Code lang="json">{`{
  "name": "file_editor",
  "arguments": {
    "path": "app.py",
    "content": "def main():\\n    print(\\"Hello World!\\")\\n    # Nested quotes require \\\\\\" escaping"
  }
}`}</Code>
        <p>
          For large codebases, complex scripts, or text containing raw regex patterns, escaping frequently fails.
          This results in JSON parsing violations, syntax errors, or corrupted/truncated writes where the agent fails to save any content.
        </p>
      </Section>

      <Section title="The Solution: SEARCH/REPLACE Conflict-Marker Blocks">
        <p>
          CodePilot solves this by decoupling file mutation from tool execution entirely. Instead of passing file
          contents as an escaped argument inside a tool call, the agent emits a raw conflict-marker block headed by
          the file's path — no JSON string wrapper, no argument at all:
        </p>
        <Code lang="text">{`app.py
<<<<<<< SEARCH
=======
def main():
    print("Hello World!")
    # Raw text is written exactly as-is.
    # Bypasses quote and newline escaping entirely!
>>>>>>> REPLACE`}</Code>
        <p>
          An empty SEARCH section means create-or-fully-overwrite. A non-empty SEARCH section must uniquely match
          the file's current content; the runtime replaces only that matched region with REPLACE.
        </p>
        <Callout>
          <strong>Advantages:</strong>
          <ul style={{ paddingLeft: 20, marginTop: 8, lineHeight: 1.8 }}>
            <li><strong>Zero Escaping Overhead:</strong> Text is sent exactly as written.</li>
            <li><strong>Less Token Usage:</strong> Eliminates character bloat caused by quote escapes and backslashes.</li>
            <li><strong>100% Reliable Writes:</strong> Bypasses JSON serialization limits entirely, guaranteeing file writes match the model's exact target output.</li>
          </ul>
        </Callout>
      </Section>
    </>
  );
}
