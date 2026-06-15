import { describe, expect, it } from 'vitest';

import { EpsMeter, RingBuffer, keeps, sampleFactor } from './sampling';

describe('sampleFactor', () => {
  it('keeps everything at or below the 200 EPS threshold', () => {
    expect(sampleFactor(0)).toBe(1);
    expect(sampleFactor(200)).toBe(1);
  });
  it('keeps every k-th above threshold, k = ceil(EPS/200)', () => {
    expect(sampleFactor(201)).toBe(2);
    expect(sampleFactor(400)).toBe(2);
    expect(sampleFactor(401)).toBe(3);
    expect(sampleFactor(1000)).toBe(5);
  });
});

describe('keeps', () => {
  it('is deterministic by arrival index (stable under re-render)', () => {
    const factor = 5;
    const kept = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].filter((i) => keeps(i, factor));
    expect(kept).toEqual([0, 5, 10]); // exactly 1/5
  });
  it('keeps all when factor is 1', () => {
    expect([0, 1, 2].every((i) => keeps(i, 1))).toBe(true);
  });
});

describe('RingBuffer', () => {
  it('retains only the last N, newest last', () => {
    const buf = new RingBuffer<number>(3);
    for (let i = 0; i < 5; i++) buf.push(i);
    expect(buf.snapshot()).toEqual([2, 3, 4]);
    expect(buf.length).toBe(3);
  });
  it('returns a fresh array each snapshot (referential change)', () => {
    const buf = new RingBuffer<number>(3);
    buf.push(1);
    expect(buf.snapshot()).not.toBe(buf.snapshot());
  });
  it('resize shrinks to the newest N, clear empties', () => {
    const buf = new RingBuffer<number>(5);
    for (let i = 0; i < 5; i++) buf.push(i);
    buf.resize(2);
    expect(buf.snapshot()).toEqual([3, 4]);
    buf.clear();
    expect(buf.length).toBe(0);
  });
});

describe('EpsMeter', () => {
  it('measures the trailing 1 s window', () => {
    const meter = new EpsMeter();
    meter.add(100, 0);
    meter.add(100, 250);
    expect(meter.rate(250)).toBe(200); // 200 events over 1 s
  });
  it('drops buckets older than the window', () => {
    const meter = new EpsMeter();
    meter.add(100, 0);
    meter.add(100, 2_000); // 2 s later → old bucket pruned
    expect(meter.rate(2_000)).toBe(100);
  });
});
