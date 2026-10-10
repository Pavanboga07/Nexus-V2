import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Same-origin dev proxy: the browser never talks to the API host
// directly, so the backend's (absent) CORS config and its WebSocket
// origin policy see ordinary same-origin traffic. The proxy forwards
// the original Host, keeping the origin check meaningful.
//
// NEXUS_DEV_API overrides the target so two frontends can drive two
// independent backends side by side during the A2A demo, e.g.
// NEXUS_DEV_API=http://127.0.0.1:8002 npm run dev -- --port 5174
const apiTarget =
  process.env.NEXUS_DEV_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
        ws: true,
      },
    },
  },
  preview: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
