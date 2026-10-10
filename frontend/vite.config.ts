import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Same-origin dev proxy: the browser never talks to the API host
// directly, so the backend's (absent) CORS config and its WebSocket
// origin policy see ordinary same-origin traffic. The proxy forwards
// the original Host, keeping the origin check meaningful.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
        ws: true,
      },
    },
  },
  preview: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
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
