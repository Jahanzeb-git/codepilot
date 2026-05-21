import { Code, Callout, Table, Section, PageHeader } from "./components";

export function PageShellTools() {
  return (
    <>
      <PageHeader
        title="Shell Tools"
        subtitle="The agent has a persistent, non-blocking shell session system powered by pexpect on Linux/macOS and pywinpty on Windows. Commands never hang the agent."
      />

      <Callout>
        A default shell session (<code>"main"</code>) starts automatically when the Runtime is created and persists
        across <code>run()</code> calls. Its PID, status, and current working directory are shown in the system prompt every step.
      </Callout>

      <Section title="execute — run a command">
        <p>Runs a command, waits up to <code>timeout</code> seconds, returns whatever output is available.</p>
        <Code lang="python">{`# LLM control block:

# status: completed — command finished within timeout (includes return_code)
execute("main", "pytest tests/ -v", 30)

# status: running — timeout hit, process still alive
execute("main", "pip install -r requirements.txt", 10)

# Spin up a server in its own terminal session, in one step
execute("server", "uvicorn app.main:app --host 0.0.0.0 --port 8000", 4, new_terminal=True)`}</Code>
      </Section>

      <Section title="read_output — wait for more output">
        <p>Called after <code>execute</code> returned <code>status: running</code>. Waits up to <code>timeout</code> seconds for new output.</p>
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li><strong>New output available:</strong> returns only the new delta (non-overlapping with previous output).</li>
          <li><strong>No new output (command already done):</strong> returns the complete accumulated output and collapses previous outputs in the context to save tokens.</li>
        </ul>
        <Code lang="python">{`# LLM control block:
read_output("main", 30)   # wait up to 30 more seconds`}</Code>
      </Section>

      <Section title="send_input — interact with prompts">
        <p>Sends text to an interactive command waiting for user input.</p>
        <Code lang="python">{`# LLM control block:
send_input("main", "yes\\n", 5)    # confirm a CLI prompt
send_input("main", "admin\\n", 5)  # enter a username`}</Code>
      </Section>

      <Section title="send_input — interrupt or send control keys">
        <Code lang="python">{`# Interrupt foreground process with Ctrl+C. The terminal session survives.
send_input("server", "\\x03", 5)

# Send Ctrl+D / EOF to exit REPLs or stdin-driven programs.
send_input("main", "\\x04", 5)`}</Code>
      </Section>

      <Section title="terminate_terminal — destroy a session">
        <Code lang="python">{`terminate_terminal("server")   # hard-kills the terminal session as a last resort`}</Code>
      </Section>

      <Section title="Full example: server + test">
        <Code lang="python">{`# Step 1 — LLM control block:
# Start server in its own terminal session, verify startup logs within 4s
execute("server", "uvicorn app.main:app --port 8000", 4, new_terminal=True)

# Step 2 — LLM control block (after seeing server startup logs):
# Run tests against the live server from main shell
execute("main", "pytest tests/test_api.py -v", 30)

# Step 3 — LLM control block (after tests pass):
# Shut server down cleanly
send_input("server", "\\x03", 5)`}</Code>
      </Section>

      <Section title="Context deduplication">
        <p>
          When <code>read_output()</code> returns in full-mode (the command is already done, no new data), it
          automatically removes the earlier outputs for that command from the conversation history and returns one
          complete, consolidated result. This keeps the agent context lean on long-running tasks.
        </p>
      </Section>

      <Section title="execute() parameters">
        <Table
          headers={["Parameter", "Description"]}
          rows={[
            [<code>session_id</code>, <>Terminal session to use. <code>"main"</code> exists by default (recreated after <code>reset()</code>).</>],
            [<code>command</code>, "Shell command string."],
            [<code>timeout</code>, "Seconds to wait. Output captured on timeout."],
            [<code>new_terminal</code>, <><code>True</code> = create and use a new terminal session in one step.</>],
            [<code>shell</code>, <><code>"bash"</code>, <code>"powershell"</code>, or <code>"cmd"</code>. Optional, for new sessions only.</>],
          ]}
        />
      </Section>
    </>
  );
}

export function PageCompletionBlock() {
  return (
    <>
      <PageHeader
        title="Completion Block"
        subtitle="The completion block is how the agent signals a task is done. Its content streams directly to the user in real time. When the runtime detects it, the agentic loop terminates."
      />

      <Section title="Why it exists">
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li><strong>No wasted step</strong> — the completion block can be combined with the action step, saving a full LLM inference call on simple tasks.</li>
          <li><strong>Real-time streaming</strong> — the completion text reaches the user as the LLM generates it, not after.</li>
          <li><strong>Natural</strong> — the agent just writes its closing message as plain text inside the fence.</li>
        </ul>
      </Section>

      <Section title="Separate final step (multi-step tasks)">
        <Code lang="text">{`All green — both fixes are solid.

\`\`\`completion
Fixed the 500 on profile email update: two bugs squashed.
(1) routes/profile.py:L42 — bare DB write had no error handling; wrapped in try/except,
now returns a proper 400 on failure.
(2) utils/validators.py:L18 — email regex was rejecting + aliases; pattern updated.
All tests pass. You're good to go.
\`\`\``}</Code>
      </Section>

      <Section title="Same-step completion (simple tasks)">
        <Code lang="text">{`Updating the timeout value.

\`\`\`codepilot
write_file("config.py", start_line=12, end_line=12, mode="edit")
\`\`\`

\`\`\`python filename=config.py
TIMEOUT = 30
\`\`\`

\`\`\`completion
Done — updated TIMEOUT from 10 to 30 seconds in config.py:L12.
\`\`\``}</Code>
      </Section>

      <Section title="Receiving it in your app">
        <p>The completion block fires the <code>FINISH</code> hook with its text as <code>summary</code>:</p>
        <Code lang="python">{`@on_finish(runtime)
def handle_finish(summary: str, **_):
    print(f"\\n{summary}\\n")
    save_to_database(summary)   # or send a notification, etc.

summary = runtime.run("Fix the login bug")
# summary == the completion block text, or None if loop ended another way`}</Code>
      </Section>
    </>
  );
}

export function PageChatMode() {
  return (
    <>
      <PageHeader
        title="Chat Mode"
        subtitle="The agent can respond to questions and explanations without executing any code."
      />

      <Section>
        <p>
          If the LLM produces a response with no <code>codepilot</code> block, the runtime treats it as a
          conversational reply: the response is fully streamed to the user and the loop exits cleanly.
        </p>
        <Code lang="python">{`runtime = Runtime("agent.yaml", stream=True)

@on_stream(runtime)
def show(text: str, **_):
    print(text, end="", flush=True)


@on_finish(runtime)
def done(summary: str, **_):
    print(f"\\n{summary}")


# Agent answers with natural markdown — no code executed, streams fully
runtime.run("How does the config loader handle missing files?")

# Agent takes action — executes code, ends with completion block
runtime.run("Add a fallback default value to the config loader")`}</Code>
        <p>
          The agent freely uses <code>python</code> blocks to display code examples in its explanations — they
          are <strong>never</strong> executed. Only <code>codepilot</code> blocks execute.
        </p>
      </Section>

      <Section title="Step awareness">
        <p>
          The agent's system prompt is refreshed every step with the current timestamp, OS, working directory,
          and a live step counter with progressive urgency:
        </p>
        <Code lang="text">{`# Steps 1-9 of 30 — neutral
Agentic step 3 / 30

# Steps 10-22 of 30 — mild signal
Agentic step 12 / 30 — 40% agentic steps consumed!

# Steps 23-26 of 30 — approaching
Agentic step 24 / 30 — 80% agentic steps consumed. Approaching step limit!

# Steps 27-30 of 30 — urgent
Agentic step 28 / 30 — 93% agentic steps consumed! Hard Limit Near!`}</Code>
      </Section>
    </>
  );
}

export function PageWorkspaceChanges() {
  return (
    <>
      <PageHeader
        title="Workspace Change Detection"
        subtitle="The runtime automatically detects when you modify files in the workspace between agent steps."
      />

      <Section>
        <p>
          If you edit a file while the agent is working, it will be notified at the start of the next step
          with exact line numbers of what changed.
        </p>
        <Code lang="text">{`[ENVIRONMENT CHANGE] 2026-02-21 16:30:12

Modified: main.py
Changed lines: 1-4, 47
Created: .env (3 lines)
Deleted: old_config.py`}</Code>
        <p>
          The agent is then instructed to re-read affected files before editing because its cached line numbers
          become stale.
        </p>
      </Section>

      <Section title="How it works">
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li>Tracking is <strong>opt-in by file</strong> — only files the agent has touched (read or written) are watched.</li>
          <li>Detection is <strong>snapshot-based</strong> — no background daemon, no file watchers, zero overhead between steps.</li>
          <li>Snapshots are taken at the end of each step and compared at the start of the next.</li>
          <li>Diff limits: 30 changed lines reported per file, 100 total across all files.</li>
        </ul>
        <Callout>No configuration is required — this is always on.</Callout>
      </Section>
    </>
  );
}
