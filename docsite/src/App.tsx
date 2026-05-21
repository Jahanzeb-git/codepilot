import { useEffect, useMemo, useState } from "react";
import {
  BookOpen,
  ChevronRight,
  Github,
  Menu,
  Moon,
  Search,
  Sun,
  X,
} from "lucide-react";
import { featureCards, navGroups, sections, stats } from "./content";

type Theme = "light" | "dark";

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const saved = window.localStorage.getItem("codepilot-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function App() {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [activeId, setActiveId] = useState(sections[0].id);
  const [menuOpen, setMenuOpen] = useState(false);
  const activeSection = useMemo(
    () => sections.find((section) => section.id === activeId) ?? sections[0],
    [activeId],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("codepilot-theme", theme);
  }, [theme]);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible?.target.id) setActiveId(visible.target.id);
      },
      { rootMargin: "-20% 0px -65% 0px", threshold: [0.1, 0.35, 0.7] },
    );

    sections.forEach((section) => {
      const node = document.getElementById(section.id);
      if (node) observer.observe(node);
    });

    return () => observer.disconnect();
  }, []);

  const closeMenu = () => setMenuOpen(false);

  return (
    <div className="site-shell">
      <Header
        theme={theme}
        menuOpen={menuOpen}
        onMenu={() => setMenuOpen((open) => !open)}
        onTheme={() => setTheme((value) => (value === "dark" ? "light" : "dark"))}
      />

      <div className="layout">
        <aside className={`sidebar ${menuOpen ? "is-open" : ""}`}>
          <div className="sidebar-top">
            <span className="sidebar-label">Documentation</span>
            <button className="icon-button mobile-only" onClick={closeMenu} aria-label="Close menu">
              <X size={18} />
            </button>
          </div>
          <nav aria-label="Documentation">
            {navGroups.map((group) => (
              <div className="nav-group" key={group.title}>
                <p>{group.title}</p>
                {group.items.map((item) => (
                  <a
                    className={item.id === activeId ? "active" : ""}
                    href={`#${item.id}`}
                    key={item.id}
                    onClick={closeMenu}
                  >
                    {item.label}
                  </a>
                ))}
              </div>
            ))}
          </nav>
        </aside>

        <main className="content">
          <Hero />
          <section className="quick-grid" aria-label="Highlights">
            {featureCards.map((card) => {
              const Icon = card.icon;
              return (
                <article className="feature-card" key={card.title}>
                  <Icon size={20} />
                  <h3>{card.title}</h3>
                  <p>{card.description}</p>
                </article>
              );
            })}
          </section>

          <div className="doc-flow">
            {sections.map((section) => {
              const Icon = section.icon;
              return (
                <section className="doc-section" id={section.id} key={section.id}>
                  <div className="section-heading">
                    <div className="section-icon">
                      <Icon size={18} />
                    </div>
                    <span>{section.eyebrow}</span>
                  </div>
                  <h2>{section.title}</h2>
                  <p>{section.description}</p>
                  {section.points ? (
                    <ul className="point-list">
                      {section.points.map((point) => (
                        <li key={point}>{point}</li>
                      ))}
                    </ul>
                  ) : null}
                  {section.code ? <CodeBlock code={section.code} /> : null}
                </section>
              );
            })}
          </div>
        </main>

        <aside className="toc" aria-label="On this page">
          <p>On This Page</p>
          <a href={`#${activeSection.id}`}>{activeSection.eyebrow ?? activeSection.title}</a>
          <div className="toc-card">
            <span>Current section</span>
            <strong>{activeSection.title}</strong>
          </div>
        </aside>
      </div>
    </div>
  );
}

function Header({
  theme,
  menuOpen,
  onMenu,
  onTheme,
}: {
  theme: Theme;
  menuOpen: boolean;
  onMenu: () => void;
  onTheme: () => void;
}) {
  return (
    <header className="topbar">
      <a className="brand" href="#introduction" aria-label="CodePilot home">
        <span className="brand-mark">
          <BookOpen size={20} />
        </span>
        <span>CodePilot</span>
      </a>

      <nav className="top-links" aria-label="Primary">
        <a href="#quick-start">Docs</a>
        <a href="#runtime">Runtime</a>
        <a href="#microvms">Architecture</a>
        <a href="#api">Reference</a>
      </nav>

      <div className="top-actions">
        <label className="search-box">
          <Search size={16} />
          <span>Search docs...</span>
          <kbd>/</kbd>
        </label>
        <a
          className="icon-button"
          href="https://github.com/Jahanzeb-git/codepilot"
          aria-label="Open GitHub repository"
        >
          <Github size={18} />
        </a>
        <button className="icon-button" onClick={onTheme} aria-label="Toggle theme">
          {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
        </button>
        <button className="icon-button mobile-only" onClick={onMenu} aria-label="Open menu">
          {menuOpen ? <X size={18} /> : <Menu size={18} />}
        </button>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section className="hero" id="top">
      <div className="breadcrumb">
        <span>Docs</span>
        <ChevronRight size={14} />
        <span>Agentic Runtime</span>
      </div>
      <div className="hero-grid">
        <div>
          <p className="eyebrow">CodePilot Documentation</p>
          <h1>Build coding agents that can actually work inside real projects.</h1>
          <p className="hero-copy">
            A quiet, fast documentation home for the CodePilot library: runtime concepts,
            tool execution, terminal multiplexing, persistence, and hosted workspace architecture.
          </p>
          <div className="hero-actions">
            <a className="primary-action" href="#quick-start">
              Quick Start
            </a>
            <a className="secondary-action" href="#microvms">
              View Architecture
            </a>
          </div>
        </div>
        <div className="hero-panel">
          <div className="panel-dots">
            <span />
            <span />
            <span />
          </div>
          <CodeBlock
            code={
              "from codepilot import AsyncRuntime\n\nruntime = AsyncRuntime('agent.yaml', stream=True)\nawait runtime.run('Fix the failing API test.')"
            }
          />
          <div className="stats-grid">
            {stats.map((stat) => (
              <div key={stat.label}>
                <span>{stat.label}</span>
                <strong>{stat.value}</strong>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function CodeBlock({ code }: { code: string }) {
  return (
    <pre className="code-block">
      <code>{code}</code>
    </pre>
  );
}
