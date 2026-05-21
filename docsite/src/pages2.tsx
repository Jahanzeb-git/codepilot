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
          <li>LLM writes a natural language reasoning paragraph (streamed to user in real time), then a <code>{"`"}codepilot{"`"}</code> block (Python code)</li>
          <li>Runtime executes the code block in a sandboxed environment with bound tool functions</li>
          <li>Execution result is appended to conversation history as <code>[EXECUTION RESULT]</code></li>
          <li>Repeat until the agent emits a <code>{"`"}completion{"`"}</code> block, hits <code>max_steps</code>, or is aborted</li>
        </ol>
      </Section>

      <Section title="The three block types">
        <Table
          headers={["Block", "Syntax", "Purpose"]}
          rows={[
            [<strong>Control Block</strong>, <code>{"`"}``codepilot{"`"}``</code>, "The only block the runtime executes. Regular python blocks are display-only markdown."],
            [<strong>Payload Blocks</strong>, <code>{"`"}``python filename=…{"`"}``</code>, "File content consumed by write_file() in order. Never executed."],
            [<strong>Completion Block</strong>, <code>{"`"}``completion{"`"}``</code>, "Natural text that streams to the user. Its presence marks the task complete."],
          ]}
        />
      </Section>

      <Section title="Action step (more work needed)">
        <Code lang="text">{`Alright, let me read the file first to get the line numbers.

\`\`\`codepilot
# Reading before editing - exact line numbers required.
read_file("routes/profile.py", start_line=35, end_line=65)
\`\`\``}</Code>
      </Section>

      <Section title="Single-step task (action + completion)">
        <Code lang="text">{`Got it, updating the timeout value.

\`\`\`codepilot
# Simple single-line edit, no read needed.
write_file("config.py", start_line=12, end_line=12, mode="edit")
\`\`\`

\`\`\`python filename=config.py
TIMEOUT = 30
\`\`\`

\`\`\`completion
Done. Updated TIMEOUT to 30s in config.py on line 12.
\`\`\``}</Code>
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
print(summary)  # the text the agent put in the completion block, or None`}</Code>
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
        <code>run()</code> returns when the agent emits a completion block, hits <code>max_steps</code>, or is
        aborted. The return value is the completion block text, or <code>None</code> if the loop ended for any
        other reason.
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
        <p>The runtime streams in two windows per step:</p>
        <ol style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li><strong>Pre-fence text</strong> — everything before the <code>{"`"}codepilot{"`"}</code> block. This is the agent's reasoning paragraph. Streams in real time as the LLM generates it.</li>
          <li><strong>Completion block</strong> — the <code>{"`"}completion{"`"}</code> block content, when the task is done. Streams in real time directly to the user. The loop terminates after this.</li>
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
