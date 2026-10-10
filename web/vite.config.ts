// Builds the MotherDuck Dive (../dives/airport_conditions/index.tsx) as a static site for
// Vercel. The Dive is imported unchanged; only its data hooks are swapped for
// src/dive-runtime.ts, which runs the same SQL on DuckDB-WASM.
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Public build-time variables use the Next.js-style prefix, which is how the Vercel
  // Supabase integration names them. Only NEXT_PUBLIC_* values reach the browser.
  envPrefix: "NEXT_PUBLIC_",
  resolve: {
    alias: {
      "@motherduck/react-sql-query": fileURLToPath(new URL("./src/dive-runtime.ts", import.meta.url)),
    },
    // The Dive lives outside web/, so resolve its imports from web/node_modules.
    dedupe: ["react", "react-dom", "recharts"],
  },
  server: {
    fs: { allow: [".."] },
    // `yarn dev` / `yarn preview` expect the API on :8000 (see README).
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  preview: { proxy: { "/api": "http://127.0.0.1:8000" } },
  optimizeDeps: { exclude: ["@duckdb/duckdb-wasm"] },
});
