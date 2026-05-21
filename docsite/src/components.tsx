import { Info, Copy, Check } from "lucide-react";
import { useState, useEffect, useRef } from "react";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import yaml from "highlight.js/lib/languages/yaml";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import typescript from "highlight.js/lib/languages/typescript";
import plaintext from "highlight.js/lib/languages/plaintext";

hljs.registerLanguage("python", python);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("json", json);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("text", plaintext);
hljs.registerLanguage("plaintext", plaintext);

const LANG_ALIAS: Record<string, string> = {
  py: "python",
  sh: "bash",
  shell: "bash",
  yml: "yaml",
  ts: "typescript",
  js: "typescript",
};

export function Code({ children, lang }: { children: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  const codeRef = useRef<HTMLElement>(null);
  const resolvedLang = lang ? (LANG_ALIAS[lang] ?? lang) : "plaintext";

  useEffect(() => {
    if (codeRef.current) {
      codeRef.current.removeAttribute("data-highlighted");
      hljs.highlightElement(codeRef.current);
    }
  }, [children, resolvedLang]);

  const copy = async () => {
    await navigator.clipboard.writeText(children);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="code-wrap">
      <div className="code-header">
        <span className="code-lang-badge">{lang ?? "text"}</span>
        <button className={`copy-btn ${copied ? "copied" : ""}`} onClick={copy} aria-label="Copy code">
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      <pre className="code-block">
        <code className={`language-${resolvedLang}`} ref={codeRef}>{children}</code>
      </pre>
    </div>
  );
}

export function Callout({ children }: { children: React.ReactNode }) {
  return (
    <div className="callout">
      <span className="callout-icon"><Info size={15} /></span>
      <div>{children}</div>
    </div>
  );
}

export function Table({ headers, rows }: { headers: string[]; rows: (string | React.ReactNode)[][] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{headers.map((h) => <th key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Points({ items }: { items: string[] }) {
  return (
    <ul className="point-list">
      {items.map((item, i) => <li key={i}>{item}</li>)}
    </ul>
  );
}

export function Section({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div className="doc-section">
      {title && <h2>{title}</h2>}
      {children}
    </div>
  );
}

export function PageHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <>
      <h1 className="page-title">{title}</h1>
      <p className="page-subtitle">{subtitle}</p>
      <hr className="page-divider" />
    </>
  );
}
