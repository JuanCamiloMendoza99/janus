import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// No dev proxy: the app talks to the API cross-origin in development and relies
// on the backend's CORS policy (CORS_ALLOW_ORIGINS), which is deliberate — it is
// the same path a deployed console on a different host would take, and it keeps
// the API base a single decision in `src/api.ts` rather than split between a
// proxy config and runtime code.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
