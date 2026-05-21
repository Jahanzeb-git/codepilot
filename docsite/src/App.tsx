import { useState, useEffect } from "react";
import { BookOpen, Moon, Sun, Github, Menu, X, ChevronRight, Info } from "lucide-react";
import { navGroups, allPages, findNav, type PageId } from "./pages";
import {
  PageIntroduction,
  PageInstallation,
  PageQuickStart,
  PageAgentFile,
  PageHowItWorks,
  PageBasicUsage,
  PageStreaming,
  PageMultiTurn,
  PageShellTools,
  PageCompletionBlock,
  PageChatMode,
  PageWorkspaceChanges,
  PageSessionPersistence,
  PageContextMemory,
  PageResumingSession,
  PageResettingSession,
  PageHooks,
  PagePermissionGating,
  PageMidTaskInjection,
  PageMultiOperation,
  PageCustomTools,
  PageAborting,
  PageCLIPattern,
  PageWebServer,
  PageAPIReference,
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
    agentfile: <PageAgentFile />,
    "how-it-works": <PageHowItWorks />,
    "basic-usage": <PageBasicUsage />,
    streaming: <PageStreaming />,
    "multi-turn": <PageMultiTurn />,
    "shell-tools": <PageShellTools />,
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
          <span className="brand-icon">
            <BookOpen size={16} />
          </span>
          CodePilot
        </a>

        <nav className="top-nav">
          <button className={page === "introduction" ? "active" : ""} onClick={() => setPage("introduction")}>Docs</button>
          <button className={page === "how-it-works" ? "active" : ""} onClick={() => setPage("how-it-works")}>How It Works</button>
          <button className={page === "api-reference" ? "active" : ""} onClick={() => setPage("api-reference")}>API Reference</button>
        </nav>

        <div className="top-actions">
          <a
            className="icon-btn"
            href="https://github.com/Jahanzeb-git/codepilot"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="GitHub"
          >
            <Github size={18} />
          </a>
          <button
            className="icon-btn"
            onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </header>

      <div className="layout">
        {/* Overlay for mobile */}
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

        <main className="main">
          <div className="page-breadcrumb">
            <span>Docs</span>
            <ChevronRight size={14} />
            <span>{nav.group}</span>
            <ChevronRight size={14} />
            <span style={{ color: "var(--text)" }}>{nav.label}</span>
          </div>

          {pageComponents[page] ?? <PageIntroduction nav={setPage} />}
        </main>
      </div>
    </div>
  );
}

// Re-export helpers used by pages
export { Info };
