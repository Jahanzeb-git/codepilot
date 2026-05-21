import { BookOpen, Github, Package, Zap } from "lucide-react";
import { Code, Callout, Table, Points, Section, PageHeader } from "./components";
import type { PageId } from "./pages";

export function PageIntroduction({ nav }: { nav: (p: PageId) => void }) {
  return (
    <>
      <div className="hero">
        <div className="hero-eyebrow">CodePilot v0.9.1</div>
        <h1 className="hero-title">Embeddable Autonomous<br />Agent Framework</h1>
        <p className="hero-desc">
          CodePilot is an <strong>Embeddable Autonomous Agent (EAA)</strong> framework for software engineering tasks.
          Embed an autonomous agent directly into your own systems: DevOps pipelines, web backends, internal tools, CLI workflows.
        </p>
        <div className="hero-actions">
          <button className="btn-primary" onClick={() => nav("quick-start")}>
            <Zap size={16} /> Quick Start
          </button>
          <a
            className="btn-secondary"
            href="https://github.com/Jahanzeb-git/codepilot"
            target="_blank"
            rel="noopener noreferrer"
          >
            <Github size={16} /> GitHub
          </a>
          <a
            className="btn-secondary"
            href="https://pypi.org/project/codepilot-ai/"
            target="_blank"
            rel="noopener noreferrer"
          >
            <Package size={16} /> PyPI
          </a>
        </div>

        <div className="hero-code">
          <span style={{ color: "#71717a" }}>$ </span>pip install codepilot-ai
        </div>

        <div className="badges" style={{ marginTop: 20 }}>
          <img src="https://img.shields.io/pypi/v/codepilot-ai" alt="PyPI version" />
          <img src="https://img.shields.io/pypi/pyversions/codepilot-ai" alt="Python" />
          <img src="https://img.shields.io/badge/license-MIT-black" alt="License" />
        </div>
      </div>

      <div className="feature-grid">
        {[
          {
            icon: <BookOpen size={18} />,
            title: "Code-as-Interface Runtime",
            desc: "The model writes executable Python in a codepilot block. No brittle JSON schemas or generic function-calling wrappers.",
          },
          {
            icon: <Zap size={18} />,
            title: "Library-First",
            desc: "Not a chatbot UI, not a hosted assistant. A library for embedding autonomous agents into your own application stack.",
          },
          {
            icon: <Package size={18} />,
            title: "Cross-Platform",
            desc: "Runs on Linux, macOS, and Windows 10 1809+ (ConPTY). Uses pexpect on Linux/macOS, pywinpty on Windows.",
          },
        ].map((c) => (
          <div className="feature-card" key={c.title}>
            <div className="feature-card-icon">{c.icon}</div>
            <h3>{c.title}</h3>
            <p>{c.desc}</p>
          </div>
        ))}
      </div>

      <Section title="What is CodePilot?">
        <p>
          CodePilot uses a <strong>code-as-interface</strong> runtime: the model streams natural language to the user,
          writes executable Python in a <code>codepilot</code> block, side-loads file payloads when needed, and
          explicitly terminates with a <code>completion</code> block.
        </p>
        <p>
          Instead of forcing the model through brittle JSON schemas or generic function-calling wrappers, the model
          writes real Python that the runtime executes directly in a sandboxed environment.
        </p>
        <Callout>
          <strong>Cross-Platform:</strong> CodePilot runs on Linux, macOS, and Windows 10 1809+ (ConPTY required).
          Linux and macOS use <code>pexpect</code> for PTY management; Windows uses <code>pywinpty</code>.
          All terminal tools — including TUI applications, interactive REPLs, and raw control sequences — work identically.
        </Callout>
      </Section>

      <Section title="Supported Providers">
        <Table
          headers={["provider", "name examples", "api_key_env"]}
          rows={[
            [<code>anthropic</code>, "claude-opus-4-5, claude-sonnet-4-5", <code>ANTHROPIC_API_KEY</code>],
            [<code>openai</code>, "gpt-4o, gpt-4-turbo", <code>OPENAI_API_KEY</code>],
            [<code>alibaba</code>, "qwen-max, qwen-plus, qwen-turbo", <code>DASHSCOPE_API_KEY</code>],
          ]}
        />
      </Section>
    </>
  );
}

export function PageInstallation() {
  return (
    <>
      <PageHeader
        title="Installation"
        subtitle="Install the library and configure your LLM provider API key."
      />
      <Section title="Install via pip">
        <p>Install the base library:</p>
        <Code lang="bash">pip install codepilot-ai</Code>
        <p>Install with database persistence support (SQLite or PostgreSQL):</p>
        <Code lang="bash">{`pip install codepilot-ai[db]
pip install psycopg2-binary   # PostgreSQL driver only`}</Code>
      </Section>

      <Section title="Set your API key">
        <p>Set your LLM provider key before running anything:</p>
        <Code lang="bash">{`# Pick one
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DASHSCOPE_API_KEY="..."`}</Code>
      </Section>

      <Section title="Supported Providers">
        <Table
          headers={["Provider", "Models", "Key env var"]}
          rows={[
            [<code>anthropic</code>, "claude-opus-4-5, claude-sonnet-4-5", <code>ANTHROPIC_API_KEY</code>],
            [<code>openai</code>, "gpt-4o, gpt-4-turbo", <code>OPENAI_API_KEY</code>],
            [<code>alibaba</code>, "qwen-max, qwen-plus, qwen-turbo", <code>DASHSCOPE_API_KEY</code>],
          ]}
        />
      </Section>
    </>
  );
}

export function PageQuickStart() {
  return (
    <>
      <PageHeader
        title="Quick Start"
        subtitle="Get a working agent running in under 5 minutes."
      />

      <Section title="1. Install">
        <Code lang="bash">pip install codepilot-ai</Code>
      </Section>

      <Section title="2. Set your API key">
        <Code lang="bash">export ANTHROPIC_API_KEY="sk-ant-..."</Code>
      </Section>

      <Section title="3. Create an agent.yaml">
        <p>
          Paths in <code>agent.yaml</code> are resolved relative to the YAML file itself, not the shell's current
          working directory. So <code>work_dir: "./workspace"</code> means a <code>workspace/</code> directory
          next to this <code>agent.yaml</code> file.
        </p>
        <Code lang="yaml">{`agent:
  name: "CodePilot"
  role: "Autonomous software engineering agent."

  model:
    provider: "anthropic"
    name: "claude-sonnet-4-5"
    api_key_env: "ANTHROPIC_API_KEY"

  runtime:
    work_dir: "./workspace"
    max_steps: 20

  tools:
    - name: "read_file"
      enabled: true
    - name: "write_file"
      enabled: true
    - name: "execute"
      enabled: true
    - name: "read_output"
      enabled: true
    - name: "send_input"
      enabled: true
    - name: "terminate_terminal"
      enabled: true
    - name: "find"
      enabled: true
    - name: "ask_user"
      enabled: true`}</Code>
      </Section>

      <Section title="4. Run synchronously">
        <Code lang="python">{`from codepilot import Runtime

runtime = Runtime("agent.yaml")
summary = runtime.run("Fix the nginx config")
print(summary)`}</Code>
      </Section>

      <Section title="5. Run asynchronously">
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

export function PageAgentFile() {
  return (
    <>
      <PageHeader
        title="AgentFile"
        subtitle="Every Runtime is driven by a YAML config. Paths are resolved relative to the YAML file's location, not the caller's CWD."
      />

      <Section title="Full configuration example">
        <Code lang="yaml">{`# agent.yaml
agent:
  name: "BackendEngineer"
  role: "Expert Python backend engineer specialising in FastAPI and PostgreSQL."

  # Either a raw string or a path to a .md file (resolved relative to this YAML)
  system_prompt: "./prompts/instructions.md"

  model:
    provider: "alibaba"             # "anthropic" | "openai" | "alibaba"
    name: "qwen-max"
    api_key_env: "DASHSCOPE_API_KEY"
    temperature: 0.2
    max_tokens: 8096
    thinking:                       # Anthropic only: extended reasoning
      enabled: false
      budget_tokens: 8000

  runtime:
    work_dir: "./workspace"         # where the agent reads/writes files
    max_steps: 30                   # hard cap on agentic steps per run()
    unsafe_mode: false              # true = allow writes outside work_dir

  tools:
    - name: "write_file"
      enabled: true
      config:
        require_permission: false   # true = ask user before every file write

    - name: "read_file"
      enabled: true

    - name: "execute"
      enabled: true
      config:
        require_permission: true    # true = ask user before every shell command
        max_output_chars: 10000     # truncate long command output

    - name: "read_output"
      enabled: true

    - name: "send_input"
      enabled: true

    - name: "terminate_terminal"
      enabled: true

    - name: "ask_user"
      enabled: true

    - name: "find"
      enabled: true

    - name: "semantic_search"
      enabled: true
      config:
        api_key_env: "VOYAGE_API_KEY"
        model: "voyage-code-3"
        base_url: "https://api.voyageai.com/v1"
        provider: "openai"
        max_results: 5
        timeout: 60
        max_output_chars: 8000`}</Code>
      </Section>

      <Callout>
        If you provide a <code>tools:</code> list, CodePilot honours it exactly. If you omit the{" "}
        <code>tools:</code> block entirely, the runtime falls back to its default built-in tool set.
      </Callout>

      <Section title="memory block (optional)">
        <Code lang="yaml">{`agent:
  memory:
    # Context window size for stress tracking and safety-net triggering
    max_context_tokens: 120000

    # Trigger global summary when usage crosses this fraction
    global_summary_threshold: 0.9

    # Max tokens for generated [GLOBAL SUMMARY] content
    global_summary_max_tokens: 500`}</Code>
      </Section>
    </>
  );
}
