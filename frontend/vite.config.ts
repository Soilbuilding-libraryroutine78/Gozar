import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Vite config for the Gozar Web_Console.
//
// In development the SPA talks to the backend admin API. The API origin is taken
// from VITE_API_BASE_URL (wired by compose.yml). When it is left empty the client
// uses same-origin requests and the dev proxy below forwards /api and /v1 to the
// backend, which avoids CORS during local `npm run dev`.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendOrigin = env.VITE_DEV_PROXY_TARGET || "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      host: true,
      port: 5173,
      proxy: {
        "/api": { target: backendOrigin, changeOrigin: true },
        "/v1": { target: backendOrigin, changeOrigin: true },
      },
    },
    preview: {
      host: true,
      port: 5173,
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});
