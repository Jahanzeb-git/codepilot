import type { EditorSettings } from "../core/types";

type Listener<T> = (v: T) => void;

/** Minimal observable value. No reducers, no middleware — just get/set/subscribe. */
export class Store<T> {
  private value: T;
  private listeners = new Set<Listener<T>>();

  constructor(initial: T) {
    this.value = initial;
  }
  get(): T {
    return this.value;
  }
  set(v: T) {
    this.value = v;
    this.listeners.forEach((l) => l(v));
  }
  update(fn: (v: T) => T) {
    this.set(fn(this.value));
  }
  subscribe(l: Listener<T>): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}

const EDITOR_KEY = "codepilot.editorSettings";
const THEME_KEY = "codepilot.theme";
const SESSIONS_KEY = "codepilot.sessions";
const ACTIVE_SESSION_KEY = "codepilot.activeSession";

function loadEditorSettings(): EditorSettings {
  const savedTheme = (localStorage.getItem(THEME_KEY) as "dark" | "light") || "dark";
  try {
    const raw = localStorage.getItem(EDITOR_KEY);
    if (raw) return { ...JSON.parse(raw), theme: savedTheme };
  } catch {
    /* ignore */
  }
  return {
    theme: savedTheme,
    fontSize: 13,
    tabSize: 2,
    wordWrap: true,
    minimap: true,
    formatOnSave: false,
  };
}

export const editorSettings = new Store<EditorSettings>(loadEditorSettings());
editorSettings.subscribe((s) => {
  localStorage.setItem(EDITOR_KEY, JSON.stringify(s));
  localStorage.setItem(THEME_KEY, s.theme);
  document.documentElement.setAttribute("data-theme", s.theme);
});

// The agent protocol has no `list_sessions` op (sessions are files in
// ~/.codepilot/sessions on the machine, outside the /workspace fs root the
// control socket exposes). We track known session ids locally so the
// switcher has something to show; the server remains the source of truth
// for a session's actual contents.
export function loadKnownSessions(): string[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return ["session_001"];
}
export function saveKnownSessions(sessions: string[]) {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions));
}
export function loadActiveSession(): string {
  return localStorage.getItem(ACTIVE_SESSION_KEY) || "session_001";
}
export function saveActiveSession(id: string) {
  localStorage.setItem(ACTIVE_SESSION_KEY, id);
}
