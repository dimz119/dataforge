import { describe, expect, it } from 'vitest';

import { TAIL_SUBPROTOCOL, isServerFrame } from './frames';

describe('frames', () => {
  it('pins the versioned subprotocol', () => {
    expect(TAIL_SUBPROTOCOL).toBe('dataforge.events.v1');
  });

  describe('isServerFrame', () => {
    it('accepts every known frame discriminator', () => {
      for (const type of ['ready', 'resume_ack', 'event', 'drop_notice', 'heartbeat', 'error']) {
        expect(isServerFrame({ type })).toBe(true);
      }
    });
    it('rejects non-objects, null, and unknown discriminators', () => {
      expect(isServerFrame(null)).toBe(false);
      expect(isServerFrame('event')).toBe(false);
      expect(isServerFrame(42)).toBe(false);
      expect(isServerFrame({ type: 'auth' })).toBe(false);
      expect(isServerFrame({})).toBe(false);
    });
  });
});
