import { TamperDetector } from '../src/tamperHeuristic';

describe('TamperDetector', () => {
  it('does not fire before enough samples are collected', () => {
    const d = new TamperDetector();
    expect(d.check(50000)).toBe(false);
    expect(d.check(50000)).toBe(false);
    expect(d.check(1000)).toBe(false);
  });

  it('fires when a frame drops far below the rolling average', () => {
    const d = new TamperDetector();
    for (let i = 0; i < 5; i++) d.check(50000);
    expect(d.check(5000)).toBe(true);
  });

  it('does not fire for ordinary variation', () => {
    const d = new TamperDetector();
    for (let i = 0; i < 5; i++) d.check(50000);
    expect(d.check(42000)).toBe(false);
  });

  it('reset clears history', () => {
    const d = new TamperDetector();
    for (let i = 0; i < 5; i++) d.check(50000);
    d.reset();
    expect(d.check(5000)).toBe(false);
  });

  it('keeps firing through a sustained tamper block instead of going silent', () => {
    const d = new TamperDetector();
    for (let i = 0; i < 5; i++) d.check(50000);
    for (let i = 0; i < 10; i++) {
      expect(d.check(5000)).toBe(true);
    }
  });
});
