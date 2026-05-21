import { Info } from "lucide-react";

export function Code({ children, lang }: { children: string; lang?: string }) {
  return (
    <div className="code-wrap">
      {lang && <span className="code-lang">{lang}</span>}
      <pre className="code-block">
        <code>{children}</code>
      </pre>
    </div>
  );
}

export function Callout({ children }: { children: React.ReactNode }) {
  return (
    <div className="callout">
      <span className="callout-icon"><Info size={16} /></span>
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
