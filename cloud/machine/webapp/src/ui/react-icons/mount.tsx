import React from "react";
import { createRoot } from "react-dom/client";

export function mountIcon(container: HTMLElement, IconComponent: React.ComponentType) {
  // Clear any existing content like SVG strings
  container.innerHTML = "";
  const root = createRoot(container);
  root.render(<IconComponent />);
  return root;
}
