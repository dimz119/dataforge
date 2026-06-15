import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});

// jsdom lacks ResizeObserver, which Radix primitives (Slider/Switch) read on mount.
// Provide a no-op so headless-component tests render. (Test-env polyfill only.)
if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = class ResizeObserver {
    constructor(callback: ResizeObserverCallback) {
      void callback;
    }
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}
