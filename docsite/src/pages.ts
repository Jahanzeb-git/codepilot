// Page IDs for routing
export type PageId =
  | "introduction"
  | "installation"
  | "quick-start"
  | "agentfile"
  | "how-it-works"
  | "basic-usage"
  | "streaming"
  | "multi-turn"
  | "session-persistence"
  | "context-memory"
  | "resuming-session"
  | "resetting-session"
  | "hooks"
  | "permission-gating"
  | "mid-task-injection"
  | "multi-operation"
  | "shell-tools"
  | "completion-block"
  | "workspace-changes"
  | "chat-mode"
  | "custom-tools"
  | "aborting"
  | "cli-pattern"
  | "web-server"
  | "api-reference";

export type NavGroup = {
  title: string;
  items: Array<{ id: PageId; label: string }>;
};

export const navGroups: NavGroup[] = [
  {
    title: "Getting Started",
    items: [
      { id: "introduction", label: "Introduction" },
      { id: "installation", label: "Installation" },
      { id: "quick-start", label: "Quick Start" },
      { id: "agentfile", label: "AgentFile" },
    ],
  },
  {
    title: "Core Concepts",
    items: [
      { id: "how-it-works", label: "How It Works" },
      { id: "basic-usage", label: "Basic Usage" },
      { id: "streaming", label: "Streaming" },
      { id: "multi-turn", label: "Multi-turn Execution" },
      { id: "shell-tools", label: "Shell Tools" },
      { id: "completion-block", label: "Completion Block" },
      { id: "chat-mode", label: "Chat Mode" },
      { id: "workspace-changes", label: "Workspace Changes" },
    ],
  },
  {
    title: "Sessions & Memory",
    items: [
      { id: "session-persistence", label: "Session Persistence" },
      { id: "context-memory", label: "Context Memory" },
      { id: "resuming-session", label: "Resuming a Session" },
      { id: "resetting-session", label: "Resetting a Session" },
    ],
  },
  {
    title: "Production",
    items: [
      { id: "hooks", label: "Hooks" },
      { id: "permission-gating", label: "Permission Gating" },
      { id: "mid-task-injection", label: "Mid-task Messages" },
      { id: "multi-operation", label: "Multi-operation Steps" },
      { id: "custom-tools", label: "Custom Tools" },
      { id: "aborting", label: "Aborting the Agent" },
      { id: "cli-pattern", label: "Building a CLI" },
      { id: "web-server", label: "Web Server Integration" },
    ],
  },
  {
    title: "Reference",
    items: [{ id: "api-reference", label: "Full API Reference" }],
  },
];

export const allPages: PageId[] = navGroups.flatMap((g) =>
  g.items.map((i) => i.id)
);

export function findNav(id: PageId): { group: string; label: string } {
  for (const g of navGroups) {
    const item = g.items.find((i) => i.id === id);
    if (item) return { group: g.title, label: item.label };
  }
  return { group: "Docs", label: id };
}
