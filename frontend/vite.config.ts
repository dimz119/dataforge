import tailwindcss from '@tailwindcss/vite';
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
    plugins: [react(), tailwindcss()],
    // Emit dist/.vite/manifest.json so the bundle-budget gate (scripts/check-bundle-size.mjs,
    // frontend-architecture §12.1) can separate the synchronous boot graph (entry +
    // vendor + shared/ui + generated client → ≤ 250 KB gzip) from the lazy route
    // chunks (the per-page-group `lazy()` splits → ≤ 150 KB gzip each, §12.2).
    build: { manifest: true },
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
      // Vitest owns the co-located unit/component suites under src/ ONLY. The
      // Playwright E2E specs in e2e/ are a separate runner (they import
      // @playwright/test, which vitest cannot resolve) — exclude them so a
      // `vitest run` never tries to collect them (frontend-architecture §11.1).
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
      // In production the API base is same-origin (''), resolved by the browser's
      // fetch against location.origin. jsdom's openapi-fetch path calls `new URL()`
      // directly, which rejects a relative URL — so tests use an absolute base.
      env: { VITE_API_BASE_URL: 'http://localhost' },
      // Coverage gates (frontend-architecture §12 / phase context):
      // shared/api + shared/ws ≥ 90% line; features ≥ 70%. Generated types
      // (IMP-5) and pure route/index wiring are excluded.
      coverage: {
        provider: 'v8',
        // `json-summary` is what the CI gate / size report can parse; `text` for
        // local runs; `html` for the drill-down report.
        reporter: ['text', 'html', 'json-summary'],
        include: ['src/**/*.{ts,tsx}'],
        exclude: [
          'src/shared/api/schema.gen.ts',
          // Type-only re-export surface (§5.1) — no runtime statements (IMP-5 sibling).
          'src/shared/api/types.ts',
          'src/**/*.test.{ts,tsx}',
          'src/**/routes.tsx',
          'src/**/index.ts',
          'src/app/main.tsx',
          'src/shared/testing/**',
          // Pages & components are covered BEHAVIOURALLY by Playwright, not vitest
          // (testing-strategy §17.2: "pages/components — no numeric gate; adding
          // low-value snapshot tests to inflate numbers is rejected in review").
          // The vitest features gate therefore measures the unit-testable feature
          // LOGIC (controlMatrix, tpsScale, overlay/overlayErrors, scopes, slug,
          // membership) + the data-layer query/mutation factories, which is the
          // §11.1 "features ≥ 70%" surface. The page/component .tsx render paths
          // are proven by the E2E suites (core-loop / stream-control / live-tail /
          // keys / a11y) against the compose stack.
          'src/features/**/pages/**',
          'src/features/**/components/**',
          // The per-feature data layer (api.ts) is TanStack Query option/mutation
          // FACTORIES — glue that issues the typed client calls and wires the
          // invalidation matrix. It carries no branching logic of its own and is
          // exercised end to end by the E2E suites (every page that reads/writes
          // goes through it) and the component tests; per §17.2 it is not a
          // snapshot-coverage target. The §11.1 "features ≥ 70%" gate measures the
          // pure, unit-testable feature LOGIC below it.
          'src/features/**/api.ts',
          // App-shell layouts & guards are likewise rendered/asserted by E2E
          // (auth.spec.ts exercises the guards + the workspace layout end to end);
          // they carry no vitest numeric gate.
          'src/app/layouts/**',
          'src/app/guards/**',
          'src/app/router.tsx',
          'src/app/WorkspaceResolver.tsx',
        ],
        // CI-enforced gates (frontend-architecture §11.1 / testing-strategy §17.2):
        // shared/api + shared/ws are the contract- and money-path → ≥ 90% line;
        // features (behaviourally covered by Playwright too) → ≥ 70% line. A drop
        // fails CI. Per-glob thresholds; `100` here would mean "exactly", so we use
        // the floor numbers from the spec. `perFile: false` ⇒ the floor is the glob
        // aggregate, matching the spec's per-area phrasing.
        thresholds: {
          'src/shared/api/**': { lines: 90 },
          'src/shared/ws/**': { lines: 90 },
          'src/features/**': { lines: 70 },
        },
      },
    },
  };
});
