import { defineConfig } from "vite";

import react from "@vitejs/plugin-react";

// Output straight into runner/frontend/dist — this is exactly what
// server/src/config.rs defaults STATIC_PATH to ("./frontend/dist",
// resolved relative to the server's cwd, /workspace-adjacent at runtime).
// See runner/Dockerfile: we COPY this dist/ next to the server binary.
export default defineConfig({
  root: __dirname,
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../runner/frontend/dist",
    emptyOutDir: true,
    target: "es2020",
    sourcemap: false,
    chunkSizeWarningLimit: 4000,
  },
  server: {
    port: 5173,
    // Proxy the three WebSocket planes + HTTP to a local `cargo run` of
    // codepilot-server during development (see webapp/README.md).
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8080", ws: true },
    },
  },
  worker: {
    format: "es",
  },
});
