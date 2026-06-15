// Prepends the IMP-5 header to the generated OpenAPI types
// (frontend-architecture §2.2 IMP-5, §5.1): the file is generated, never hand-edited,
// and excluded from lint + coverage. The drift gate (gen:api:check) regenerates and
// diffs it, so this header is regenerated deterministically each run.
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, '../src/shared/api/schema.gen.ts');

const HEADER = [
  '/* eslint-disable */',
  '/**',
  ' * GENERATED — DO NOT EDIT (frontend-architecture §2.2 IMP-5, §5.1).',
  ' * Source of truth: backend/schema/openapi.yaml. Regenerate with `npm run gen:api`.',
  ' * The CI drift gate (`npm run gen:api:check`) fails on any FE/BE schema drift (ADR-0014).',
  ' */',
  '',
].join('\n');

const body = readFileSync(target, 'utf8');
if (!body.startsWith('/* eslint-disable */')) {
  writeFileSync(target, HEADER + body, 'utf8');
}
