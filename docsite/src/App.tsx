import { useState, useEffect } from "react";
import { Moon, Sun, Github, Menu, X, ChevronRight, BookOpen, ExternalLink } from "lucide-react";
import { navGroups, allPages, findNav, type PageId } from "./pages";
import {
  PageIntroduction, PageInstallation, PageQuickStart, PageModelsProviders, PageAgentFile,
  PageHowItWorks, PageBasicUsage, PageCodeAsInterface, PageStreaming, PageMultiTurn,
  PageTerminalTools, PageFileHandling, PageSearchTools, PageContextArchiving, PageUserInteraction,
  PageCompletionBlock, PageChatMode, PageWorkspaceChanges, PageMcpSupport,
  PageSessionPersistence, PageContextMemory, PageResumingSession, PageResettingSession,
  PageHooks, PagePermissionGating, PageMidTaskInjection, PageMultiOperation,
  PageCustomTools, PageAborting, PageCLIPattern, PageWebServer, PageAPIReference,
} from "./PageContent";
import "./styles.css";

type Theme = "light" | "dark";

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const saved = localStorage.getItem("cp-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function getInitialPage(): PageId {
  const hash = window.location.hash.slice(1) as PageId;
  return allPages.includes(hash) ? hash : "introduction";
}

export function App() {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [page, setPage] = useState<PageId>(getInitialPage);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [logoError, setLogoError] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("cp-theme", theme);
  }, [theme]);

  useEffect(() => {
    window.location.hash = page;
    window.scrollTo({ top: 0, behavior: "instant" });
    setSidebarOpen(false);
  }, [page]);

  useEffect(() => {
    const onHash = () => {
      const h = window.location.hash.slice(1) as PageId;
      if (allPages.includes(h)) setPage(h);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const nav = findNav(page);

  const pageComponents: Record<PageId, JSX.Element> = {
    introduction: <PageIntroduction nav={setPage} />,
    installation: <PageInstallation />,
    "quick-start": <PageQuickStart />,
    "models-providers": <PageModelsProviders />,
    agentfile: <PageAgentFile />,
    "how-it-works": <PageHowItWorks />,
    "basic-usage": <PageBasicUsage />,
    "code-as-interface": <PageCodeAsInterface />,
    streaming: <PageStreaming />,
    "multi-turn": <PageMultiTurn />,
    "file-handling": <PageFileHandling />,
    "search-tools": <PageSearchTools />,
    "terminal-tools": <PageTerminalTools />,
    "context-archiving": <PageContextArchiving />,
    "user-interaction": <PageUserInteraction />,
    "mcp-support": <PageMcpSupport />,
    "completion-block": <PageCompletionBlock />,
    "chat-mode": <PageChatMode />,
    "workspace-changes": <PageWorkspaceChanges />,
    "session-persistence": <PageSessionPersistence />,
    "context-memory": <PageContextMemory />,
    "resuming-session": <PageResumingSession />,
    "resetting-session": <PageResettingSession />,
    hooks: <PageHooks />,
    "permission-gating": <PagePermissionGating />,
    "mid-task-injection": <PageMidTaskInjection />,
    "multi-operation": <PageMultiOperation />,
    "custom-tools": <PageCustomTools />,
    aborting: <PageAborting />,
    "cli-pattern": <PageCLIPattern />,
    "web-server": <PageWebServer />,
    "api-reference": <PageAPIReference />,
  };

  return (
    <div className="site">
      {/* ── Topbar ── */}
      <header className="topbar">
        <button
          className="icon-btn mobile-menu-btn"
          onClick={() => setSidebarOpen((o) => !o)}
          aria-label="Toggle sidebar"
        >
          {sidebarOpen ? <X size={18} /> : <Menu size={18} />}
        </button>

        <a
          className="brand"
          href="#introduction"
          onClick={(e) => { e.preventDefault(); setPage("introduction"); }}
        >
          {!logoError ? (
            <img
              src="/codepilot/codepilot.png"
              alt="CodePilot logo"
              className="brand-logo"
              onError={() => setLogoError(true)}
            />
          ) : (
            <span className="brand-icon"><BookOpen size={15} /></span>
          )}
          CodePilot
        </a>

        <nav className="top-nav">
          <button className={page === "introduction" ? "active" : ""} onClick={() => setPage("introduction")}>Docs</button>
          <button className={page === "how-it-works" ? "active" : ""} onClick={() => setPage("how-it-works")}>How It Works</button>
          <button className={page === "api-reference" ? "active" : ""} onClick={() => setPage("api-reference")}>API Reference</button>
        </nav>

        <div className="top-actions">
          <a className="icon-btn" href="https://github.com/Jahanzeb-git/codepilot" target="_blank" rel="noopener noreferrer" aria-label="GitHub">
            <Github size={17} />
          </a>
          <button className="icon-btn" onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))} aria-label="Toggle theme">
            {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
        </div>
      </header>

      {/* ── Body: sidebar flush left + main content ── */}
      <div className="site-body">
        {/* Mobile overlay */}
        <div
          className={`sidebar-overlay ${sidebarOpen ? "open" : ""}`}
          onClick={() => setSidebarOpen(false)}
        />

        <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
          {navGroups.map((group) => (
            <div className="sidebar-section" key={group.title}>
              <div className="sidebar-label">{group.title}</div>
              {group.items.map((item) => (
                <button
                  key={item.id}
                  className={`sidebar-link ${page === item.id ? "active" : ""}`}
                  onClick={() => setPage(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </aside>

        {/* Main + Footer share the remaining column */}
        <div className="main-wrapper">
          <main className="main">
            <div className="page-breadcrumb">
              <span>Docs</span>
              <ChevronRight size={13} />
              <span>{nav.group}</span>
              <ChevronRight size={13} />
              <span style={{ color: "var(--text)" }}>{nav.label}</span>
            </div>
            {pageComponents[page] ?? <PageIntroduction nav={setPage} />}
          </main>

          <Footer />
        </div>
      </div>
    </div>
  );
}

function Footer() {
  return (
    <>
      <footer className="footer">
        <div className="footer-col">
          <div className="footer-col-title">Getting Started</div>
          <a href="#introduction">Introduction</a>
          <a href="#installation">Installation</a>
          <a href="#quick-start">Quick Start</a>
          <a href="#models-providers">Models & Providers</a>
          <a href="#agentfile">AgentFile</a>
        </div>
        <div className="footer-col">
          <div className="footer-col-title">Core Concepts</div>
          <a href="#how-it-works">How It Works</a>
          <a href="#code-as-interface">Code-as-Interface</a>
          <a href="#streaming">Streaming</a>
          <a href="#completion-block">Completion Block</a>
        </div>
        <div className="footer-col">
          <div className="footer-col-title">Sessions</div>
          <a href="#session-persistence">Session Persistence</a>
          <a href="#context-memory">Context Memory</a>
          <a href="#resuming-session">Resuming a Session</a>
          <a href="#resetting-session">Resetting a Session</a>
        </div>
        <div className="footer-col">
          <div className="footer-col-title">Production</div>
          <a href="#hooks">Hooks</a>
          <a href="#permission-gating">Permission Gating</a>
          <a href="#terminal-tools">Terminal Tools</a>
          <a href="#custom-tools">Custom Tools</a>
          <a href="#web-server">Web Server Integration</a>
        </div>
        <div className="footer-col">
          <div className="footer-col-title">Links</div>
          <a href="https://github.com/Jahanzeb-git/codepilot" target="_blank" rel="noopener noreferrer">
            GitHub <ExternalLink size={11} style={{ display: "inline", verticalAlign: "middle" }} />
          </a>
          <a href="https://pypi.org/project/codepilot-ai/" target="_blank" rel="noopener noreferrer">
            PyPI <ExternalLink size={11} style={{ display: "inline", verticalAlign: "middle" }} />
          </a>
          <a href="#api-reference">API Reference</a>
          <a href="https://github.com/Jahanzeb-git/codepilot/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">MIT License</a>
        </div>
      </footer>
      <div className="footer-bottom">
        <span>
          Built by{" "}
          <a href="https://github.com/Jahanzeb-git" target="_blank" rel="noopener noreferrer">
            Jahanzeb Ahmed
          </a>{" "}
          and the community.
        </span>
        <span>CodePilot v0.9.19 · MIT License</span>
      </div>
    </>
  );
}
