import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = process.env.GROK_API ?? "http://127.0.0.1:8800";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": target,
      "/ws": { target: target.replace("http", "ws"), ws: true },
    },
  },
  build: { chunkSizeWarningLimit: 900 },
});
