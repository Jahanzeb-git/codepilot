import { Code, Callout, Table, Section, PageHeader } from "./components";

export function PageSessionPersistence() {
  return (
    <>
      <PageHeader
        title="Session Persistence"
        subtitle="Session backends are chosen at construction time. Choose the right backend for your use case."
      />

      <Section title="Backend comparison">
        <Table
          headers={["Backend", "Storage", "Survives restart", "Best for"]}
          rows={[
            [<code>"memory"</code>, "RAM only", "No", "Scripts, one-off tasks"],
            [<code>"file"</code>, "~/.codepilot/sessions/", "Yes", "CLI tools, local dev"],
            [<code>"db"</code>, "Any SQL database", "Yes", "Web apps, containers, multi-user"],
          ]}
        />
      </Section>

      <Section title="In-memory (default)">
        <Code lang="python">{`runtime = Runtime("agent.yaml")                          # memory, id = agent name
runtime = Runtime("agent.yaml", session="memory")       # explicit, same thing
runtime = Runtime("agent.yaml", session="memory", session_id="my-session")`}</Code>
      </Section>

      <Section title="File-backed">
        <p>
          History is serialised to <code>~/.codepilot/sessions/&lt;session_id&gt;.json</code> after every{" "}
          <code>run()</code>. Directory is created automatically.
        </p>
        <Code lang="python">{`runtime = Runtime("agent.yaml", session="file")                     # id = agent name
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# Custom session directory
from pathlib import Path
runtime = Runtime(
    "agent.yaml",
    session="file",
    session_id="ecommerce-api",
    session_dir=Path("/data/codepilot-sessions"),
)`}</Code>
        <p>Session file format:</p>
        <Code lang="json">{`{
  "session_id": "ecommerce-api",
  "agent_name": "BackendEngineer",
  "created_at": 1712345678.0,
  "updated_at": 1712349999.0,
  "messages": [ ... ],
  "extra": {
    "memory_state": { ... }
  }
}`}</Code>
        <p>
          The <code>extra</code> field stores runtime-owned session state such as archived-context memory, so
          a resumed session restores more than just raw message history.
        </p>
      </Section>

      <Section title="Database-backed">
        <p>
          Persists history to any SQLAlchemy-compatible database. The <code>codepilot_sessions</code> table
          is created automatically — no migration scripts needed.
        </p>
        <Code lang="bash">{`# Install the db extras
pip install codepilot-ai[db]          # SQLite or PostgreSQL
pip install psycopg2-binary           # PostgreSQL driver only`}</Code>
        <Code lang="python">{`# SQLite — simple, zero-config, great for local persistence
runtime = Runtime(
    "agent.yaml",
    session="db",
    session_id="user-42",
    db_url="sqlite:///./codepilot.db",
)

# PostgreSQL — for containers, Cloud Run, multi-user apps
import os
runtime = Runtime(
    "agent.yaml",
    session="db",
    session_id=f"user-{user_id}",
    db_url=os.environ["DATABASE_URL"],
)`}</Code>
      </Section>

      <Section title="Persistence behaviour">
        <Table
          headers={["Moment", "What happens"]}
          rows={[
            [<code>Runtime(...)</code> + "construction", "One SELECT loads prior messages for the session_id, or [] for new sessions"],
            ["Each run() call", "All agentic steps run fully in-memory with zero DB I/O during inference"],
            ["run() completes", "One atomic UPSERT writes the full messages list plus runtime extra state"],
            ["New Runtime(...) same session_id", "One SELECT — session fully restored"],
            [<code>runtime.reset()</code>, "DELETE row — clean slate"],
          ]}
        />
      </Section>

      <Section title="Listing all sessions">
        <Code lang="python">{`from codepilot import DatabaseSession

ds = DatabaseSession(session_id="_", db_url="sqlite:///./codepilot.db")
for s in ds.list_sessions():
    print(f"{s['session_id']:30} {s['messages']:4} messages")`}</Code>
      </Section>
    </>
  );
}

export function PageContextMemory() {
  return (
    <>
      <PageHeader
        title="Context Memory Management"
        subtitle="CodePilot uses agent-driven context control with a global safety net."
      />

      <Section title="How it works">
        <ol style={{ paddingLeft: 20, color: "var(--text-soft)", lineHeight: 2 }}>
          <li>The agent can explicitly archive finished tasks using <code>archive_context(...)</code>. The original task messages are stored internally and replaced with <code>[ARCHIVED TASK N]</code> plus your summary.</li>
          <li>The agent can restore any archived task using <code>reveal_context(N)</code>.</li>
          <li>A global safety net runs at the start of each <code>run()</code>: if context exceeds <code>global_summary_threshold * max_context_tokens</code>, older history is collapsed into one <code>[GLOBAL SUMMARY]</code> message.</li>
        </ol>
      </Section>

      <Section title="What the LLM sees in long sessions">
        <Code lang="text">{`[GLOBAL SUMMARY]            <- oldest history compressed by safety net
[ARCHIVED TASK 3]           <- explicit archive summary created by agent
[ARCHIVED TASK 4]           <- explicit archive summary created by agent
[Task 5][USER INPUT] ...    <- active task, raw`}</Code>
      </Section>

      <Section title="Context tools (used from control block)">
        <Code lang="python">{`# Archive one task
archive_context(task=3, summary="Implemented auth middleware and passing tests.")

# Backward-compatible argument name
archive_context(position=4, summary="Added user routes and validation.")

# Archive multiple tasks in one call
archive_context(
    task=(1, 2),
    summary=[
        "Initialized FastAPI project layout.",
        "Added SQLAlchemy models for users and sessions."
    ]
)

# Reveal archived task content
reveal_context(3)

# List archived tasks with token savings
list_archived_context()`}</Code>
      </Section>

      <Section title="Configuration (agent.yaml)">
        <Code lang="yaml">{`agent:
  memory:
    max_context_tokens: 120000
    global_summary_threshold: 0.9
    global_summary_max_tokens: 500`}</Code>
      </Section>
    </>
  );
}

export function PageResumingSession() {
  return (
    <>
      <PageHeader
        title="Resuming a Session"
        subtitle="Pass the same session_id to a new file-backed Runtime and the prior conversation loads automatically."
      />

      <Section>
        <Code lang="python">{`# Process 1
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
runtime.run("Create the products and orders FastAPI endpoints")
# Process exit — session saved

# -------- later, new process --------

# Process 2 picks up exactly where process 1 left off
runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")
runtime.run("Add database migrations using Alembic")`}</Code>
      </Section>

      <Section title="Listing saved sessions">
        <Code lang="python">{`from codepilot import FileSession

fs = FileSession(session_id="_", agent_name="_")
for s in fs.list_sessions():
    print(f"{s['session_id']:30} {s['messages']:4} messages  updated {s['updated_at']}")`}</Code>
      </Section>

      <Section title="Inspecting a session without loading messages">
        <Code lang="python">{`from codepilot import FileSession

fs = FileSession(session_id="ecommerce-api", agent_name="BackendEngineer")
meta = fs.metadata()
if meta:
    print(f"Last updated: {meta['updated_at']}")
    print(f"File path: {fs.path}")
else:
    print("No saved session, will start fresh")`}</Code>
      </Section>
    </>
  );
}

export function PageResettingSession() {
  return (
    <>
      <PageHeader
        title="Resetting a Session"
        subtitle="Wipes all history and deletes the session file (if file-backed). The next run() starts completely fresh."
      />

      <Section>
        <Code lang="python">{`runtime = Runtime("agent.yaml", session="file", session_id="ecommerce-api")

# ... some runs ...

runtime.reset()
runtime.run("Start over — build a GraphQL API instead")`}</Code>
      </Section>
    </>
  );
}
