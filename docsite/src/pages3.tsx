import { Code, Callout, Table, Section, PageHeader } from "./components";

export function PageTerminalTools() {
  return (
    <>
      <PageHeader
        title="Terminal Tools"
        subtitle="The agent has a persistent, non-blocking virtual terminal session system. Commands never hang the agent."
      />

      <Callout>
        A default shell session (<code>"main"</code>) starts automatically when the Runtime is created and persists
        across <code>run()</code> calls. Its PID, status, and current working directory are shown in the system prompt every step.
      </Callout>

      <Section title="Architecture & PTY Multiplexing">
        <p>
          Unlike naive agent frameworks that run command strings using generic <code>subprocess.run</code>, CodePilot features a custom <strong>Virtual Terminal Emulator</strong> built specifically for LLMs.
        </p>
        <h4 style={{ color: "var(--text)", marginTop: 16 }}>Key Features:</h4>
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li><strong>Cross-Platform Support:</strong> CodePilot natively runs pseudo-terminal (PTY) emulation across major operating systems—supporting <strong>Linux and macOS</strong> (via <code>pexpect</code>) and <strong>Windows</strong> (via <code>pywinpty</code> for Windows 10 version 1809+ using ConPTY).</li>
          <li><strong>Garbage Byte Filtering:</strong> Automatically strips out ANSI escape sequences, raw control bytes, and binary clutter to present a clean text layout.</li>
          <li><strong>Virtual 220x50 Grid:</strong> Renders a raw snapshot of the terminal screen buffer exactly as a human would see it.</li>
          <li><strong>Never Hangs:</strong> The agent never blocks or hangs due to long-running or interactive shell processes. On timeout, the virtual terminal captures the current screen buffer snapshot and returns it immediately along with rich metadata (PID, return code, session status, and CWD).</li>
        </ul>

        <h4 style={{ color: "var(--text)", marginTop: 16 }}>Socket Multiplexing Under the Hood:</h4>
        <p>
          CodePilot spawns a custom multiplexer daemon. It manually maps the slave end of a pseudo-terminal (PTY) to the shell process (e.g. <code>bash</code>) and maps the master end to a Unix domain socket.
        </p>
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li>A global control socket listens at <code>/tmp/codepilot_control.sock</code> to broadcast discovery and lifecycle events (e.g. <code>{"{ \"event\": \"terminal_created\", \"session_id\": \"main\", \"socket_path\": \"/tmp/codepilot_main.sock\" }"}</code>).</li>
          <li>The CodePilot virtual terminal connects to the session socket (e.g. <code>/tmp/codepilot_main.sock</code>) to interact.</li>
          <li><strong>Shared Emulation:</strong> External frontend terminal emulators (like <code>xterm.js</code>) can connect to the same session socket in parallel. This allows a single, live shell session to be shared dynamically between the CodePilot agent and the developer.</li>
        </ul>
      </Section>

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

# Agent takes action — calls task(finish=True) in its control block
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

export function PageFileHandling() {
  return (
    <>
      <PageHeader
        title="File Handling Tools"
        subtitle="Manage reading and editing files safely in the agent environment."
      />

      <Section title="read_file">
        <p>Reads a range of lines from a file. Preserves token budget by avoiding reading full large files unnecessarily.</p>
        <Code lang="python">{`# LLM control block:
read_file("app/models.py", start_line=1, end_line=50)`}</Code>
        <Table
          headers={["Parameter", "Type", "Description"]}
          rows={[
            [<code>path</code>, "str", "Relative path to file in workspace."],
            [<code>start_line</code>, "int", "1-based starting line number (default: 1)."],
            [<code>end_line</code>, "int", "Ending line number. If omitted, reads to EOF (max 800 lines)."],
          ]}
        />
      </Section>

    </>
  );
}

export function PageSearchTools() {
  return (
    <>
      <PageHeader
        title="Search Tools"
        subtitle="Locate files and search code syntactically and semantically."
      />

      <Section title="find">
        <p>Performs a fast, literal or regular expression ripgrep search inside files in the workspace.</p>
        <Code lang="python">{`# LLM control block:
find("class ModelConfig", "codepilot/")`}</Code>
        <Table
          headers={["Parameter", "Type", "Description"]}
          rows={[
            [<code>query</code>, "str", "Search string or regex pattern."],
            [<code>path</code>, "str", "Workspace subdirectory to search (optional)."],
          ]}
        />
      </Section>

      <Section title="semantic_search">
        <p>Uses vector embeddings to locate code blocks semantically related to a natural language query.</p>
        <Code lang="python">{`# LLM control block:
semantic_search("how is the token count calculated for DeepSeek requests?", max_results=3)`}</Code>
        <Table
          headers={["Parameter", "Type", "Description"]}
          rows={[
            [<code>query</code>, "str", "Natural language search query."],
            [<code>max_results</code>, "int", "Maximum number of matched snippets to return (default: 5)."],
          ]}
        />
      </Section>
    </>
  );
}

export function PageContextArchiving() {
  return (
    <>
      <PageHeader
        title="Context Archiving Tools"
        subtitle="CodePilot protects the next model call by compacting completed-task history before the context window is exhausted."
      />

      <Section title="When maintenance starts">
        <p>
          Before every inference, CodePilot measures the rendered system prompt, existing conversation history,
          the configured maximum next response, and the safety margin. When Context Stress reaches the configured
          trigger, the runtime gives the same agent a maintenance-only turn. The active task is protected.
        </p>
        <Code lang="text">{`safe history budget = context window
                    - system prompt
                    - model.max_tokens
                    - thinking budget (when enabled)
                    - safety margin`}</Code>
        <p>
          The temporary <code>archive_context</code> tool is available only during that maintenance turn. If no completed
          task can be archived and physical load reaches 93% of the safe history budget, the runtime uses emergency global summarization.
        </p>
      </Section>

      <Section title="archive_context">
        <p>
          The agent chooses semantically irrelevant completed tasks, stores their original messages in session-owned archive state,
          and replaces their live history with a concise factual summary. The tool result reports saved tokens and the remeasured stress.
        </p>
        <Code lang="python">{`# LLM control block:
archive_context(position=2, summary="Completed database migration setup. Files created: migrations/001_init.py")`}</Code>
        <Table
          headers={["Parameter", "Type", "Description"]}
          rows={[
            [<code>position</code>, "int or tuple", "Completed task index or indices. The active task cannot be archived."],
            [<code>summary</code>, "str or list", "Dense factual summary: files, decisions, commands, outcomes, and unresolved items."],
          ]}
        />
      </Section>

      <Section title="What remains in the prompt">
        <Code lang="text">{`[ARCHIVED TASK 2]
Implemented the migration. Changed migrations/001_init.py and db.py.
Tests: pytest tests/db -q passed. No unresolved items relevant to the active task.

[Task 3][USER INPUT]
Continue with the active task...`}</Code>
        <p>
          Archived originals remain in persisted archive state even if an emergency global summary later consumes their live placeholder.
          Reasoning tags are removed from global summaries before they are stored.
        </p>
      </Section>

    </>
  );
}

export function PageUserInteraction() {
  return (
    <>
      <PageHeader
        title="User Interaction Tools"
        subtitle="Pause agent execution to request input or clarification from the operator."
      />

      <Section title="ask_user">
        <p>
          Pauses agent execution and prompts the user with a question.
          In a CLI environment, it falls back to a blocking stdin prompt.
          In production web applications, it emits an <code>ASK_USER</code> event that can be captured and answered asynchronously.
        </p>
        <Code lang="python">{`# LLM control block:
ask_user("Should I use PostgreSQL or SQLite for the testing database configuration?")`}</Code>
        <Table
          headers={["Parameter", "Type", "Description"]}
          rows={[
            [<code>question</code>, "str", "The clarification question to present to the user."],
          ]}
        />
      </Section>
    </>
  );
}

export function PageMcpSupport() {
  return (
    <>
      <PageHeader
        title="Model Context Protocol (MCP)"
        subtitle="CodePilot natively supports connecting to external MCP servers to massively expand its toolset without polluting the LLM's context window."
      />

      <Section title="What is MCP?">
        <p>
          The <a href="https://modelcontextprotocol.io/" target="_blank" rel="noreferrer">Model Context Protocol (MCP)</a> is an open standard developed by Anthropic that standardises how AI models access data and tools. 
          It allows you to securely expose local databases, external APIs (like GitHub or Slack), and file systems to CodePilot using a universal JSON-RPC 2.0 protocol.
        </p>
      </Section>

      <Section title="Client Specifications">
        <p>
          CodePilot implements a robust, universal MCP client architecture under the hood. 
        </p>
        <Table
          headers={["Specification", "Value"]}
          rows={[
            ["Client Name", <code>codepilot_mcp_client:v1</code>],
            ["Client Version", <code>1.0.0</code>],
            ["JSON-RPC Version", <code>2.0</code>],
            ["Protocol Versions", <><code>2025-06-18</code> (Modern) with auto-downgrade to <code>2024-11-05</code> (Legacy)</>],
          ]}
        />
        
        <h4 style={{ color: "var(--text)", marginTop: 16 }}>Universal HTTP Transport</h4>
        <p>
          While the JSON-RPC messages are standardised, different MCP servers use different HTTP transport architectures. CodePilot automatically negotiates and supports both major HTTP transport modes transparently:
        </p>
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 1.6 }}>
          <li>
            <strong>Streamable HTTP (Modern)</strong>: Used by platforms like Tavily. Every request is a simple, synchronous <code>POST</code> direct to the main endpoint, returning the JSON-RPC result in the response body or as a short-lived SSE stream.
          </li>
          <li>
            <strong>Legacy True SSE</strong>: Used by strict servers like the official GitHub Copilot MCP. CodePilot automatically opens an asynchronous, permanent <code>GET</code> stream, waits for an <code>endpoint</code> routing event, and handles bi-directional JSON-RPC mapping via background <code>asyncio</code> tasks.
          </li>
        </ul>
        <Callout>
          You don't need to configure transport types manually! CodePilot automatically detects if the server requires True SSE or Streamable HTTP during the initialization handshake.
        </Callout>
      </Section>

      <Section title="Embedding MCP: Solving the Context Window">
        <p>
          A major flaw in naive MCP implementations is that they inject the schemas for <em>every</em> discovered tool directly into the LLM's system prompt. If you connect to an enterprise MCP server exposing 500 internal APIs, your agent will immediately run out of context tokens before it even starts working.
        </p>
        <p>
          <strong>CodePilot solves this using Embedding MCP.</strong>
        </p>
        <p>
          During the initialization handshake, CodePilot fetches all available tools from your configured MCP servers and securely embeds their schemas into a local vector database using Voyage AI (e.g. <code>voyage-code-3</code>).
        </p>
        <p>
          Instead of injecting 500 tools into the system prompt, CodePilot injects exactly <strong>two</strong> meta-tools:
        </p>
        <ul style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 1.6 }}>
          <li><code>mcp_discover</code>: Allows the LLM to semantically search for external tools based on its current task (e.g. <em>"Get the latest PR"</em>).</li>
          <li><code>mcp_invoke</code>: Allows the LLM to call the external tool once it has discovered its exact JSON schema.</li>
        </ul>
      </Section>

      <Section title="Configuration Example">
        <p>
          You can configure multiple remote MCP servers in your <code>agent.yaml</code>. CodePilot seamlessly routes authentication tokens to either HTTP Headers (for strings like <code>Authorization</code>) or Query Parameters based entirely on the <code>api_key_param</code> you specify.
        </p>
        <Code lang="yaml">{`tools:
  - name: "mcp"
    enabled: true
    config:
      embedding_model: "voyage-code-3"
      embedding_api_key_env: "VOYAGE_API_KEY"
      embedding_base_url: "https://api.voyageai.com/v1"
      top_k: 3
      servers:
        - name: "github-cloud"
          url: "https://api.githubcopilot.com/mcp/"
          api_key_env: "GITHUB_PAT"
          api_key_param: "Authorization" # Routed securely as a Bearer Token HTTP Header`}</Code>
      </Section>
    </>
  );
}
