import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// No API proxy here, unlike the other two frontend apps: this site never calls a
// backend at runtime. Every image it shows was rendered offline by
// scripts/build_demo_assets.py and baked into the static build.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5174,
  },
  preview: {
    host: true,
    port: 4174,
  },
});
