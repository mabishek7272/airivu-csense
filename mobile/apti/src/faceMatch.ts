/**
 * Cosine face matching, ported from `backend/shared/csense_shared/faces/matching.py`.
 * That module stores/decodes base64 templates for the real product's encrypted-at-rest
 * gallery; this one has no encoding step because embeddings here never leave app memory -
 * only `normalise` and the matching math are needed client-side.
 */

export const EMBEDDING_DIM = 512;
export const DEFAULT_THRESHOLD = 0.4;

export interface GalleryEntry {
  id: string;
  name: string;
  vector: number[]; // unit length, EMBEDDING_DIM long
}

export interface Match {
  id: string;
  name: string;
  similarity: number;
}

function dot(a: number[], b: number[]): number {
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += a[i] * b[i];
  return sum;
}

/** ai-runtime's `/internal/v1/infer-face` already L2-normalises its embeddings; this
 *  re-normalises defensively rather than trusting that on every caller. */
export function normalise(vector: number[]): number[] {
  if (vector.length !== EMBEDDING_DIM) {
    throw new Error(`expected a ${EMBEDDING_DIM}-d embedding, got ${vector.length}`);
  }
  const length = Math.sqrt(dot(vector, vector));
  if (!Number.isFinite(length) || length === 0) {
    throw new Error('embedding has zero length or is not finite');
  }
  return vector.map((v) => v / length);
}

/** The single most similar gallery entry at or above `threshold`, or null. An identity
 *  may appear several times (one row per registered photo); the entry with the highest
 *  score wins, exactly like the Python `best_match` this mirrors. */
export function bestMatch(
  embedding: number[],
  gallery: GalleryEntry[],
  threshold: number = DEFAULT_THRESHOLD,
): Match | null {
  if (gallery.length === 0) return null;
  const query = normalise(embedding);
  let best: Match | null = null;
  for (const candidate of gallery) {
    const similarity = dot(query, candidate.vector);
    if (best === null || similarity > best.similarity) {
      best = { id: candidate.id, name: candidate.name, similarity };
    }
  }
  if (best === null || best.similarity < threshold) return null;
  return { ...best, similarity: Math.round(best.similarity * 10000) / 10000 };
}
