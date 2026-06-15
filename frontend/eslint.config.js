import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import boundaries from 'eslint-plugin-boundaries';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules', 'eslint.config.js', 'scripts/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  jsxA11y.flatConfigs.recommended,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
      globals: { ...globals.browser },
    },
  },
  {
    files: ['vite.config.ts', 'vitest.setup.ts'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    // Playwright E2E specs + config live outside src/ and tsconfig.json's `include`,
    // so the typed-lint project service cannot resolve them. They are a separate
    // program (tsconfig.e2e.json typechecks them in CI); here we lint them with the
    // recommended (non-type-checked) rule set and the boundaries/IMP rules off —
    // those target the app source graph, not the test harness. Node + browser
    // globals: specs run in Node (Playwright runner) and evaluate browser code.
    files: ['e2e/**/*.ts', 'playwright.config.ts'],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: {
      parserOptions: { projectService: false, project: false },
      globals: { ...globals.node, ...globals.browser },
    },
  },
  {
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
    },
  },
  {
    // Import boundaries IMP-1…IMP-3 (frontend-architecture §2.2) — CI-enforced.
    files: ['src/**/*.{ts,tsx}'],
    plugins: { boundaries },
    settings: {
      'import/resolver': { typescript: { alwaysTryTypes: true } },
      'boundaries/dependency-nodes': ['import', 'dynamic-import', 'export'],
      'boundaries/include': ['src/**/*'],
      'boundaries/elements': [
        { type: 'app', pattern: 'src/app', mode: 'folder' },
        { type: 'feature', pattern: 'src/features/*', mode: 'folder', capture: ['featureName'] },
        { type: 'shared', pattern: 'src/shared', mode: 'folder' },
      ],
    },
    rules: {
      'boundaries/element-types': [
        'error',
        {
          default: 'disallow',
          message:
            'Import boundary violation (frontend-architecture §2.2 IMP-1…IMP-3): ' +
            '${file.type} may not import ${dependency.type}.',
          rules: [
            { from: 'app', allow: ['app', 'feature', 'shared'] },
            {
              from: 'feature',
              allow: ['shared', ['feature', { featureName: '${from.featureName}' }]],
            },
            { from: 'shared', allow: ['shared'] },
          ],
        },
      ],
    },
  },
  {
    // IMP-4: only shared/api/client.ts calls fetch; only shared/ws/socket.ts constructs
    // WebSocket. dangerouslySetInnerHTML is lint-banned (frontend-architecture §6.1).
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/shared/api/client.ts', 'src/shared/ws/socket.ts'],
    rules: {
      'no-restricted-globals': [
        'error',
        { name: 'fetch', message: 'IMP-4: only shared/api/client.ts may call fetch.' },
        { name: 'WebSocket', message: 'IMP-4: only shared/ws/socket.ts may construct WebSocket.' },
      ],
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: 'dangerouslySetInnerHTML is banned (frontend-architecture §6.1).',
        },
      ],
    },
  },
  prettier,
);
