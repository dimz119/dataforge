import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

/**
 * Dev server contract (deployment-architecture §2.1, `web` row): Vite dev server on
 * 0.0.0.0:5173 proxying `/api → http://api:8000` and `/ws → ws://ws:8001` inside the
 * compose network. The targets are env-overridable so local non-Docker development can
 * point at e.g. http://localhost:8000 / ws://localhost:8001:
 *
 *   VITE_DEV_API_PROXY_TARGET=http://localhost:8000 \
 *   VITE_DEV_WS_PROXY_TARGET=ws://localhost:8001 npm run dev
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const apiProxyTarget = env.VITE_DEV_API_PROXY_TARGET ?? 'http://api:8000';
  const wsProxyTarget = env.VITE_DEV_WS_PROXY_TARGET ?? 'ws://ws:8001';

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target: apiProxyTarget, changeOrigin: true },
        '/ws': { target: wsProxyTarget, ws: true },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./vitest.setup.ts'],
    },
  };
});
