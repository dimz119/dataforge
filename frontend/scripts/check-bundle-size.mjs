#!/usr/bin/env node
// @ts-check
/**
 * Bundle-budget gate (frontend-architecture §12.1; phase-07 P7-15).
 *
 * Enforces the two static budgets the build can prove without a browser:
 *   - Initial JS (the entry chunk + everything it statically imports — vendor,
 *     shared/ui, the generated client — i.e. the synchronous boot graph), gzip ≤ 250 KB.
 *   - Any lazy route chunk (the per-page-group `lazy()` splits, §12.2), gzip ≤ 150 KB.
 *
 * The third §12.1 budget (no main-thread task > 50 ms during the tail at 1,000 TPS)
 * is a RUNTIME budget — it is proven by the Playwright tracing assertion in
 * e2e/live-tail.spec.ts against the compose stack, NOT here (a static size check
 * cannot observe main-thread tasks). This script owns the two byte budgets only.
 *
 * Run after `vite build` (which emits to dist/). Reads Vite's
 * dist/.vite/manifest.json to classify the entry boot graph vs the lazy route
 * chunks, gzips each emitted .js asset, and fails (exit 1) on any breach. Without
 * a build present it exits non-zero with a hint (so CI ordering bugs are loud).
 */
import { gzipSync } from 'node:zlib';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const KB = 1024;
const ENTRY_BUDGET = 250 * KB; // §12.1 initial JS (entry + vendor), gzip
const ROUTE_BUDGET = 150 * KB; // §12.1 any lazy route chunk, gzip

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(ROOT, 'dist');
const MANIFEST = join(DIST, '.vite', 'manifest.json');

/** @param {string} p */
function gzipKb(p) {
  return gzipSync(readFileSync(p)).length;
}

/** @param {number} bytes */
function fmt(bytes) {
  return `${(bytes / KB).toFixed(1)} KB`;
}

if (!existsSync(DIST)) {
  console.error('size:check — dist/ not found. Run `npm run build` first.');
  process.exit(1);
}

/**
 * @typedef {{ file: string; isEntry?: boolean; isDynamicEntry?: boolean; imports?: string[] }} Chunk
 * @typedef {Record<string, Chunk>} Manifest
 */

/** @returns {Manifest | null} */
function loadManifest() {
  if (!existsSync(MANIFEST)) return null;
  return /** @type {Manifest} */ (JSON.parse(readFileSync(MANIFEST, 'utf8')));
}

const manifest = loadManifest();
const failures = [];
const report = [];

if (manifest) {
  // The boot graph = the entry chunk plus the transitive closure of its *static*
  // `imports` (vendor, shared/ui, generated client). `dynamicImports` are the
  // lazy route splits and are intentionally excluded from the initial budget.
  /** @type {Set<string>} */
  const bootFiles = new Set();
  /** @param {string} key */
  const walk = (key) => {
    const chunk = manifest[key];
    if (!chunk || bootFiles.has(chunk.file)) return;
    bootFiles.add(chunk.file);
    for (const imp of chunk.imports ?? []) walk(imp);
  };
  for (const [key, chunk] of Object.entries(manifest)) {
    if (chunk.isEntry) walk(key);
  }

  let entryBytes = 0;
  for (const file of bootFiles) {
    if (!file.endsWith('.js')) continue;
    entryBytes += gzipKb(join(DIST, file));
  }
  report.push(`entry (boot graph, gzip): ${fmt(entryBytes)} / ${fmt(ENTRY_BUDGET)}`);
  if (entryBytes > ENTRY_BUDGET) {
    failures.push(`Initial JS ${fmt(entryBytes)} exceeds the ${fmt(ENTRY_BUDGET)} budget.`);
  }

  // Every lazy route chunk (a chunk not in the boot graph) is budgeted at 150 KB.
  for (const chunk of Object.values(manifest)) {
    if (!chunk.file.endsWith('.js') || bootFiles.has(chunk.file)) continue;
    const bytes = gzipKb(join(DIST, chunk.file));
    report.push(`route chunk ${chunk.file} (gzip): ${fmt(bytes)} / ${fmt(ROUTE_BUDGET)}`);
    if (bytes > ROUTE_BUDGET) {
      failures.push(`Route chunk ${chunk.file} ${fmt(bytes)} exceeds the ${fmt(ROUTE_BUDGET)} budget.`);
    }
  }
} else {
  // Fallback when the manifest is absent (manifest disabled): we cannot reliably
  // separate the boot graph from lazy chunks, so apply the looser route budget to
  // every emitted JS asset and warn. Enable `build.manifest` for the precise gate.
  console.warn(
    'size:check — dist/.vite/manifest.json absent; enable `build.manifest: true` in vite.config.ts for the precise entry/route split. Falling back to the per-file route budget.',
  );
  const assetsDir = join(DIST, 'assets');
  const files = existsSync(assetsDir) ? readdirSync(assetsDir).filter((f) => f.endsWith('.js')) : [];
  for (const f of files) {
    const bytes = gzipKb(join(assetsDir, f));
    report.push(`asset ${f} (gzip): ${fmt(bytes)} / ${fmt(ROUTE_BUDGET)}`);
    if (bytes > ROUTE_BUDGET) {
      failures.push(`Asset ${f} ${fmt(bytes)} exceeds the ${fmt(ROUTE_BUDGET)} budget.`);
    }
  }
}

console.log(report.join('\n'));

if (failures.length > 0) {
  console.error('\nBundle budget FAILED (frontend-architecture §12.1):');
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log('\nBundle budgets OK (entry ≤ 250 KB, route chunks ≤ 150 KB gzip).');
