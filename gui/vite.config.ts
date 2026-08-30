import { fileURLToPath, URL } from "node:url";
import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The dev server proxies /v1 to `mfp serve` so the browser only ever talks to
// one origin. That is not cosmetic: the loopback guard (O-3) refuses a
// cross-site Origin, so a direct fetch from :5173 to :47821 would be a 403.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": {
        target: "http://127.0.0.1:47821",
        changeOrigin: true,
        // SSE must not be buffered, or progress arrives in one lump at the end.
        ws: false,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/shared/lib/test-setup.ts"],
    css: false,
    // `e2e/` belongs to Playwright: those specs import its `test`, need a
    // browser that does layout, and mean nothing under jsdom. Without this
    // vitest collects them by filename and fails on the import.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
