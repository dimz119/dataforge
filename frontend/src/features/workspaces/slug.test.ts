import { describe, expect, it } from 'vitest';

import { deriveSlug } from './slug';

describe('deriveSlug', () => {
  it('lowercases and hyphenates', () => {
    expect(deriveSlug('Acme Cohort 2026')).toBe('acme-cohort-2026');
  });

  it('strips leading/trailing separators and non-alphanumerics', () => {
    expect(deriveSlug('  Hello, World!  ')).toBe('hello-world');
  });

  it('caps the length', () => {
    expect(deriveSlug('a'.repeat(80)).length).toBe(50);
  });
});
