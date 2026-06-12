import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import boundaries from 'eslint-plugin-boundaries';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules', 'eslint.config.js'] },
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
