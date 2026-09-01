import { BookOpen, Github, Package, Zap } from "lucide-react";
import { Code, Callout, Table, Points, Section, PageHeader } from "./components";
import { TerminalDemo } from "./TerminalDemo";
import type { PageId } from "./pages";

export function PageIntroduction({ nav }: { nav: (p: PageId) => void }) {
  return (
    <>
      <div className="hero">
        <div className="hero-flex">
          {/* ── Left: text content ── */}
          <div className="hero-content">
            <div className="hero-eyebrow">CodePilot v0.9.37</div>
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

          {/* ── Right: terminal demo (desktop only) ── */}
          <div className="hero-terminal-wrap">
            <TerminalDemo />
          </div>
        </div>
      </div>

      <div className="feature-grid">
        {[
          {
            icon: <BookOpen size={18} />,
            title: "Code-as-Interface Runtime",
            desc: "The model expresses every action as a SEARCH/REPLACE conflict-marker block. No brittle JSON schemas or generic function-calling wrappers.",
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
          then emits one or more SEARCH/REPLACE conflict-marker blocks — each headed by a <code>&lt;path&gt;</code> line —
          to mutate workspace files or run code via the ephemeral <code>codepilot.py</code> block, and
          completes work by calling <code>task(finish=True)</code> inside that script.
        </p>
        <p>
          Instead of forcing the model through brittle JSON schemas or generic function-calling wrappers, the model
          emits raw conflict-marker text that the runtime applies directly against the current file content.
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
            [<code>deepseek</code>, "deepseek-v4-pro, deepseek-v4-flash", <code>DEEPSEEK_API_KEY</code>],
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
export DEEPSEEK_API_KEY="sk-..."
export DASHSCOPE_API_KEY="..."`}</Code>
      </Section>

      <Section title="Supported Providers">
        <Table
          headers={["Provider", "Models", "Key env var"]}
          rows={[
            [<code>anthropic</code>, "claude-opus-4-5, claude-sonnet-4-5", <code>ANTHROPIC_API_KEY</code>],
            [<code>openai</code>, "gpt-4o, gpt-4-turbo", <code>OPENAI_API_KEY</code>],
            [<code>deepseek</code>, "deepseek-v4-pro, deepseek-v4-flash", <code>DEEPSEEK_API_KEY</code>],
            [<code>alibaba</code>, "qwen-max, qwen-plus, qwen-turbo", <code>DASHSCOPE_API_KEY</code>],
          ]}
        />
      </Section>
    </>
  );
}

export function PageModelsProviders() {
  return (
    <>
      <PageHeader
        title="Models & Providers"
        subtitle="Configure the LLM providers and models that power your autonomous agents."
      />
      
      <Section title="Supported Providers">
        <p>
          CodePilot supports four LLM providers out of the box. Providers are selected in your <code>agent.yaml</code> configuration file.
        </p>
        <Table
          headers={["Provider", "Key env var", "Thinking Mode", "Caching Mode"]}
          rows={[
            [<code>anthropic</code>, <code>ANTHROPIC_API_KEY</code>, "Adaptive / Manual", "Explicit (Rolling Breakpoints)"],
            [<code>openai</code>, <code>OPENAI_API_KEY</code>, "Reasoning Effort", "Automatic (Server-side)"],
            [<code>deepseek</code>, <code>DEEPSEEK_API_KEY</code>, "Reasoning Effort", "Automatic (Server-side)"],
            [<code>alibaba</code>, <code>DASHSCOPE_API_KEY</code>, "Explicit toggling", "Explicit (Rolling Breakpoints)"],
          ]}
        />
      </Section>

      <Section title="Anthropic">
        <p>
          Anthropic's Claude models provide the best overall performance with CodePilot. Newer generations support adaptive thinking.
        </p>
        
        <h3>Adaptive Thinking (Claude 4.6, 4.7+)</h3>
        <p>
          Newer Claude models determine their own thinking budget based on the complexity of the request and a specified effort level.
        </p>
        <Code lang="yaml">{`agent:
  model:
    provider: "anthropic"
    name: "claude-4-7-opus"
    api_key_env: "ANTHROPIC_API_KEY"
    thinking:
      enabled: true
      reasoning_effort: "xhigh"  # 'low', 'medium', 'high', 'xhigh', or 'max'`}</Code>

        <h3>Manual Extended Thinking (Claude 3.7, 4.5)</h3>
        <p>
          Older thinking-enabled models require a hard-capped token budget for internal reasoning.
        </p>
        <Code lang="yaml">{`agent:
  model:
    provider: "anthropic"
    name: "claude-4-5-sonnet"
    api_key_env: "ANTHROPIC_API_KEY"
    thinking:
      enabled: true
      budget_tokens: 8000`}</Code>
      </Section>

      <Section title="OpenAI">
        <p>
          OpenAI's reasoning models (like <code>gpt-5.5</code>, <code>gpt-5.4</code>, <code>o3-mini</code>) are configured via the <code>reasoning_effort</code> parameter.
        </p>
        <p>
          Note: Since the OpenAI Chat Completions API performs reasoning server-side and does not stream intermediate thought tokens, no <code>&lt;thinking&gt;</code> blocks will appear in the stream. However, the model will generate higher-quality answers based on the configured effort.
        </p>
        <Code lang="yaml">{`agent:
  model:
    provider: "openai"
    name: "gpt-5.5"
    api_key_env: "OPENAI_API_KEY"
    thinking:
      enabled: true
      reasoning_effort: "high"   # 'low', 'medium', or 'high'`}</Code>
      </Section>

      <Section title="DeepSeek">
        <p>
          DeepSeek's models (<code>deepseek-v4-pro</code> and <code>deepseek-v4-flash</code>) offer flagship reasoning performance at a very competitive price. 
        </p>
        <p>
          DeepSeek includes automatic server-side context caching, so no special TTL or breakpoints need to be configured in CodePilot.
        </p>
        <Code lang="yaml">{`agent:
  model:
    provider: "deepseek"
    name: "deepseek-v4-pro"
    api_key_env: "DEEPSEEK_API_KEY"
    thinking:
      enabled: true
      reasoning_effort: "high"   # 'high' or 'max'`}</Code>
      </Section>

      <Section title="Alibaba (Qwen)">
        <p>
          Alibaba Cloud's Qwen models (like <code>qwen-max</code>) support an explicit thinking mode toggled via DashScope parameters.
        </p>
        <Code lang="yaml">{`agent:
  model:
    provider: "alibaba"
    name: "qwen-max"
    api_key_env: "DASHSCOPE_API_KEY"
    thinking:
      enabled: true`}</Code>
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
    - name: "file_editor"
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
        <code>run()</code> returns when the agent calls <code>task(finish=True)</code>, hits <code>max_steps</code>, or is
        aborted. The return value is the agent's final natural-language text, or <code>None</code> if it ended another way.
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
    provider: "anthropic"           # "anthropic" | "openai" | "deepseek" | "alibaba"
    name: "claude-4-5-sonnet"
    api_key_env: "ANTHROPIC_API_KEY"
    temperature: 1.0
    max_tokens: 8192
    thinking:                       # Anthropic only: extended reasoning
      enabled: true
      budget_tokens: 8000

  memory:
    # The selected model's total input + output context window.
    max_context_tokens: 128000
    context_safety_margin_tokens: 1024
    context_stress_multiplier: 1.0
    context_stress_trigger: 0.78

  runtime:
    work_dir: "./workspace"         # where the agent reads/writes files
    max_steps: 30                   # hard cap on agentic steps per run()
    unsafe_mode: false              # true = allow writes outside work_dir

  tools:
    - name: "file_editor"
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
        max_output_chars: 8000
        
    - name: "mcp"
      enabled: true
      config:
        embedding_model: "voyage-code-3"
        embedding_api_key_env: "VOYAGE_API_KEY"
        embedding_base_url: "https://api.voyageai.com/v1"
        top_k: 3
        servers:
          - name: "tavily-mcp"
            url: "https://mcp.tavily.com/mcp/?tavilyApiKey=..."
            # For query parameter keys
            api_key_env: "TAVILY_API_KEY"
            api_key_param: "tavilyApiKey"
            
          - name: "github-cloud"
            url: "https://api.githubcopilot.com/mcp/"
            # For header tokens (automatically adds 'Bearer ' prefix if needed)
            api_key_env: "GITHUB_PAT"
            api_key_param: "Authorization"`}</Code>
      </Section>

      <Callout>
        If you provide a <code>tools:</code> list, CodePilot honours it exactly. If you omit the{" "}
        <code>tools:</code> block entirely, the runtime falls back to its default built-in tool set.
      </Callout>

      <Section title="Context memory defaults">
        <p>
          When a verified exact model profile is available, CodePilot fills an omitted <code>max_context_tokens</code>
          and uses its recommended response cap. For an unrecognised model, the AgentFile must set
          <code>max_context_tokens</code> explicitly; CodePilot never guesses capacity from a provider name.
        </p>
        <Code lang="text">{`safe history = max_context_tokens
             - rendered system prompt
             - model.max_tokens
             - thinking.budget_tokens (when enabled)
             - context_safety_margin_tokens`}</Code>
        <Table
          headers={["Provider / model", "Context window", "Default max_tokens"]}
          rows={[
            [<><code>openai/gpt-4o</code></>, "128,000", <code>8,192</code>],
            [<><code>anthropic/claude-fable-5</code>, <code>anthropic/claude-opus-5</code>, <code>anthropic/claude-sonnet-5</code></>, "1,000,000", <code>8,192</code>],
            [<><code>anthropic/claude-haiku-4-5</code></>, "200,000", <code>8,192</code>],
            [<><code>alibaba/qwen-max</code></>, "32,768", <code>8,192</code>],
            [<><code>deepseek/deepseek-chat</code>, <code>deepseek/deepseek-reasoner</code></>, "65,536", <code>8,192</code>],
            [<><code>alibaba/deepseek-v4-flash</code>, <code>alibaba/deepseek-v4-pro</code>, <code>deepseek/deepseek-v4-flash</code>, <code>deepseek/deepseek-v4-pro</code></>, "1,000,000", <code>8,192</code>],
          ]}
        />
        <Callout>
          The profile registry is deliberately small and exact because provider model limits change independently. For every other model,
          set its documented total context window explicitly. Keep <code>context_safety_margin_tokens: 1024</code>, <code>context_stress_multiplier: 1.0</code>, and
          <code>context_stress_trigger: 0.78</code> unless you have measured a reason to tune them. Do not set
          <code>model.max_tokens</code> higher than the response you actually need.
        </Callout>
      </Section>
    </>
  );
}
