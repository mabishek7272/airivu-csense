import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    // Proxy so the dev server is same-origin with the API, exactly like the deployed
    // stack behind Traefik. Without this, dev would need CORS and would lose the
    // SameSite refresh cookie on reload — a difference that hides real bugs until deploy.
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: false },
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
  preview: {
    host: true,
    port: 4173,
  },
});
