import { Code, Table, Section, PageHeader } from "./components";

export function PageAPIReference() {
  return (
    <>
      <PageHeader
        title="Full API Reference"
        subtitle="Complete reference for Runtime, AsyncRuntime, hook decorators, session backends, and built-in tools."
      />

      <Section title="Runtime / AsyncRuntime">
        <Code lang="python">{`Runtime(
    agent_file: str,              # path to agent.yaml
    session: str = "memory",      # "memory" | "file" | "db"
    session_id: str = None,       # defaults to agent name, slugified
    session_dir: Path = None,     # override ~/.codepilot/sessions/
    stream: bool = False,         # True = token-by-token streaming
    db_url: Optional[str] = None, # database URL (SQLite, PostgreSQL, MySQL, etc.)
)

# AsyncRuntime() also exists for async operations. Expects same arguments.

runtime.run(task: str) -> Optional[str]
    # Blocking. Appends to history. Returns completion block text or None.

runtime.send_message(message: str)
    # Thread-safe. Non-blocking. Tagged [USER MESSAGE] in context.

runtime.reset()
    # Wipes messages + session file. Next run() is a blank slate.

runtime.abort()
    # Sets abort flag. Loop stops after current step.

runtime.register_tool(name: str, func: callable, replace: bool = False)
    # Add custom tool. Docstring injected into system prompt automatically.

runtime.messages           # List[Dict] - full conversation history
runtime.session            # BaseSession - current session backend instance
runtime.hooks              # HookSystem - register/emit events manually
runtime.registry           # ToolRegistry - inspect registered tools`}</Code>
      </Section>

      <Section title="Hook decorators">
        <Code lang="python">{`from codepilot import (
    on_stream,                  # STREAM - pre-fence reasoning text or completion block content
    on_tool_call,               # TOOL_CALL - before any tool executes
    on_tool_result,             # TOOL_RESULT - after any tool returns
    on_ask_user,                # ASK_USER - agent called ask_user()
    on_finish,                  # FINISH - task complete (completion block detected)
    on_permission_request,      # PERMISSION_REQUEST - awaiting approval
    on_user_message_queued,     # USER_MESSAGE_QUEUED - send_message() called
    on_user_message_injected,   # USER_MESSAGE_INJECTED - message in context
)`}</Code>
      </Section>

      <Section title="write_file()">
        <Code lang="python">{`write_file(path, start_line=None, end_line=None, after_line=None, mode='w', edits=None)`}</Code>
        <Table
          headers={["mode", "Behaviour", "Limit"]}
          rows={[
            [<code>'w'</code>, "Create or overwrite the whole file", "5 per step"],
            [<code>'a'</code>, "Append to end of file", "5 per step (shared with 'w')"],
            [<code>'edit'</code>, "Replace lines start_line to end_line", "1 per file per step"],
            [<code>'insert'</code>, "Insert after after_line (0 = top of file)", "1 per file per step"],
            [<code>'multi_edit'</code>, "edits=[(s1,e1),(s2,e2)]. Runtime applies bottom-to-top.", "1 per file per step"],
          ]}
        />
        <p style={{ marginTop: 12 }}>Content always comes from the next payload block; never pass it as a string argument.</p>
      </Section>

      <Section title="read_file()">
        <Code lang="python">{`read_file(path, start_line=1, end_line=None)`}</Code>
        <p>Returns file content with 1-indexed line numbers. Multiple calls per step are allowed.</p>
      </Section>

      <Section title="execute()">
        <Code lang="python">{`execute(session_id, command, timeout=10, new_terminal=False, shell=None)`}</Code>
        <p>Runs a command on a persistent terminal session. Returns captured output up to timeout seconds.</p>
        <Table
          headers={["Parameter", "Description"]}
          rows={[
            [<code>session_id</code>, <>"main" exists by default (recreated after reset()).</>],
            [<code>command</code>, "Shell command string."],
            [<code>timeout</code>, "Seconds to wait. Output captured on timeout."],
            [<code>new_terminal</code>, "True = create and use a new terminal session in one step."],
            [<code>shell</code>, <>"bash", "powershell", or "cmd". Optional, for new sessions only.</>],
          ]}
        />
        <p style={{ marginTop: 12 }}>Result includes <code>status: completed</code> (done, has <code>return_code</code>) or <code>status: running</code> (timed out, process alive). Command results also include the shell <code>cwd</code>.</p>
      </Section>

      <Section title="read_output()">
        <Code lang="python">{`read_output(session_id, timeout=5)`}</Code>
        <p>Read new output from the latest command. Returns delta (new content only) or full accumulated output if the command is already done. Full-mode collapses previous outputs from context automatically.</p>
      </Section>

      <Section title="send_input()">
        <Code lang="python">{`send_input(session_id, text, timeout=5)`}</Code>
        <p>Send text to an interactive command waiting for input. Returns new output after sending.</p>
      </Section>

      <Section title="terminate_terminal()">
        <Code lang="python">{`terminate_terminal(session_id)`}</Code>
        <p>Hard-kill a terminal session. Prefer <code>send_input(session_id, "\\x03")</code> first so Ctrl+C can shut down the foreground process cleanly.</p>
      </Section>

      <Section title="ask_user()">
        <Code lang="python">{`ask_user(question)`}</Code>
        <p>Pauses execution and prompts the user for input. Fires the <code>ASK_USER</code> hook.</p>
      </Section>

      <Section title="archive_context() / reveal_context() / list_archived_context()">
        <Code lang="python">{`archive_context(position=None, summary=None, task=None)
# Archive completed task context with your summary. task is an alias for position.

reveal_context(position)
# Restore a previously archived task's full original context.

list_archived_context()
# List archived tasks with summary previews and estimated token savings.`}</Code>
      </Section>

      <Section title="find()">
        <Code lang="python">{`find(pattern, scope='codebase', target=None, include=None, max_results=50)`}</Code>
        <p>Text/regex search across a file, multiple files, or the entire workspace. Results are returned as <code>file:line:matched_line</code>.</p>
        <p>Uses <strong>ripgrep</strong> (<code>rg</code>) when available, honoring <code>.gitignore</code> automatically. Falls back to a pure-Python implementation when <code>rg</code> is not installed.</p>
        <Table
          headers={["Parameter", "Description"]}
          rows={[
            [<code>pattern</code>, <><code>r'validate_email\\('</code> — escape special chars</>],
            [<code>scope</code>, <><code>'file'</code> / <code>'files'</code> / <code>'codebase'</code></>],
            [<code>target</code>, "File path (str) or list of paths; required for scope='file'/'files'"],
            [<code>include</code>, <><code>'*.py'</code>, <code>'tests/**'</code> — glob filter for scope='codebase'</>],
            [<code>max_results</code>, "Cap on returned matches (default 50)"],
          ]}
        />
        <Code lang="python">{`# LLM control block examples:
find(pattern=r'validate_email\\(', scope='file', target='routes/profile.py')
find(pattern='TODO:', scope='files', target=['routes/profile.py', 'utils/validators.py'])
find(pattern=r'class \\w+Handler', scope='codebase', include='*.py')
find(pattern='import torch', scope='codebase', include='tests/**')`}</Code>
        <Code lang="bash">{`# Install ripgrep for best performance (optional Python fallback is always available):
apt-get install ripgrep      # Debian/Ubuntu
brew install ripgrep          # macOS`}</Code>
      </Section>

      <Section title="semantic_search()">
        <Code lang="python">{`semantic_search(query, mode='search', depth=2, top_k=5)`}</Code>
        <p>Semantically searches the codebase using the <code>voyage-code-3</code> embedding model via <a href="https://github.com/yoanbernabeu/grepai" target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>grepai</a>. Finds code by concept — not text match.</p>
        <p><strong>Requires</strong> <code>VOYAGE_API_KEY</code> set in environment and <code>api_key_env: "VOYAGE_API_KEY"</code> in the AgentFile config.</p>
        <Table
          headers={["mode", "What it does"]}
          rows={[
            [<code>'search'</code>, "Find files/functions matching a natural language concept"],
            [<code>'trace_callers'</code>, "Find every place that calls a given function/method"],
            [<code>'trace_callees'</code>, "Find everything a function calls internally"],
            [<code>'trace_graph'</code>, "Full dependency tree up to depth levels; use before modifying code with wide blast radius"],
          ]}
        />
      </Section>

      <Section title="FileSession">
        <Code lang="python">{`FileSession(session_id, agent_name, session_dir=None)

.load() -> List[Dict]          # load messages from disk
.save(messages)                # persist messages to disk (atomic write)
.reset()                       # delete session file
.exists() -> bool              # True if file exists on disk
.metadata() -> Optional[Dict]  # session metadata without messages
.list_sessions() -> List[Dict] # all sessions in the session directory
.path -> Path                  # full path to the session file
.session_id -> str`}</Code>
      </Section>

      <Section title="InMemorySession">
        <Code lang="python">{`InMemorySession(session_id="default")

.load() -> List[Dict]
.save(messages)
.reset()
.session_id -> str`}</Code>
      </Section>

      <Section title="create_session">
        <Code lang="python">{`create_session(
    backend: str = "memory",     # "memory" | "file" | "db"
    session_id: str = "default",
    agent_name: str = "agent",
    session_dir: Path = None,
    db_url: Optional[str] = None,
) -> BaseSession`}</Code>
      </Section>
    </>
  );
}
