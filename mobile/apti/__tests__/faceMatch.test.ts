import { EMBEDDING_DIM, bestMatch, normalise, type GalleryEntry } from '../src/faceMatch';

// A tiny deterministic PRNG (mulberry32) so "seed 1" always produces the same vector,
// the same role numpy's seeded default_rng plays in the Python test this mirrors
// (backend/tests/test_face_matching.py).
function mulberry32(seed: number) {
  let s = seed;
  return function () {
    s |= 0;
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function unit(seed: number): number[] {
  const rand = mulberry32(seed);
  const v: number[] = [];
  for (let i = 0; i < EMBEDDING_DIM; i++) {
    const u1 = Math.max(rand(), 1e-9);
    const u2 = rand();
    v.push(Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2));
  }
  return normalise(v);
}

function entry(name: string, seed: number): GalleryEntry {
  return { id: `id-${name}`, name, vector: unit(seed) };
}

describe('normalise', () => {
  it('rejects a zero vector', () => {
    expect(() => normalise(new Array(EMBEDDING_DIM).fill(0))).toThrow();
  });

  it('rejects the wrong dimension', () => {
    expect(() => normalise(new Array(10).fill(1))).toThrow();
  });

  it('produces a unit-length vector', () => {
    const v = normalise(unit(1).map((x) => x * 7.5));
    const length = Math.sqrt(v.reduce((sum, x) => sum + x * x, 0));
    expect(length).toBeCloseTo(1, 5);
  });
});

describe('bestMatch', () => {
  it('matches a known person by name', () => {
    const gallery = [entry('sara', 1), entry('abel', 2)];
    const match = bestMatch(unit(1), gallery);
    expect(match?.name).toBe('sara');
    expect(match!.similarity).toBeGreaterThan(0.99);
  });

  it('a stranger matches nobody', () => {
    const gallery = [entry('sara', 1), entry('abel', 2)];
    expect(bestMatch(unit(99), gallery)).toBeNull();
  });

  it('an empty gallery matches nobody', () => {
    expect(bestMatch(unit(5), [])).toBeNull();
  });

  it('respects the threshold boundary', () => {
    const a = unit(1);
    const b = unit(2);
    const mixed = normalise(a.map((v, i) => v + 0.6 * b[i]));
    const sim = mixed.reduce((sum, v, i) => sum + v * a[i], 0);
    expect(bestMatch(mixed, [entry('sara', 1)], sim - 0.01)).not.toBeNull();
    expect(bestMatch(mixed, [entry('sara', 1)], sim + 0.01)).toBeNull();
  });

  it('picks the best of several templates for one identity', () => {
    const u1 = unit(1);
    const u9 = unit(9);
    const near: GalleryEntry = { id: 'id-x', name: 'x', vector: normalise(u1.map((v, i) => v + 0.1 * u9[i])) };
    const far: GalleryEntry = { id: 'id-x', name: 'x', vector: unit(7) };
    const match = bestMatch(u1, [far, near]);
    expect(match?.id).toBe('id-x');
    expect(match!.similarity).toBeGreaterThan(0.9);
  });
});
