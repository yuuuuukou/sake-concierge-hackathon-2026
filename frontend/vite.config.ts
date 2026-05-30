import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": apiProxyTarget,
        "/chat": apiProxyTarget,
        "/health": apiProxyTarget
      }
    },
    build: {
      outDir: "../backend/src/api/static/react",
      emptyOutDir: true
    },
    test: {
      environment: "jsdom",
      globals: true,
      include: ["tests/unit/**/*.{test,spec}.{ts,tsx}"],
      setupFiles: "./src/test/setup.ts",
      testTimeout: 15_000
    }
  };
});
