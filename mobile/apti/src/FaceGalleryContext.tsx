import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { bestMatch, type GalleryEntry, type Match } from './faceMatch';

interface FaceGalleryValue {
  entries: GalleryEntry[];
  /** Distinct registered people (an identity may have several template rows). */
  personCount: number;
  /** Registers one captured photo's embedding under `name`, sharing `id` across a
   *  person's several photos - mirrors the real product's "several templates, one
   *  identity" gallery shape, so matching picks whichever template scores best. */
  register: (id: string, name: string, vector: number[]) => void;
  /** A stricter check than `match` - used by the registration wizard to warn "this looks
   *  like someone already registered" before saving a duplicate. */
  findDuplicate: (vector: number[]) => Match | null;
  match: (vector: number[]) => Match | null;
  clear: () => void;
}

const FaceGalleryContext = createContext<FaceGalleryValue | null>(null);

// Stricter than the 0.4 matching threshold - a warning, not the pass/fail bar for "is
// this the same person" during live detection.
const DUPLICATE_THRESHOLD = 0.6;

export function FaceGalleryProvider({ children }: { children: React.ReactNode }) {
  const [entries, setEntries] = useState<GalleryEntry[]>([]);

  const register = useCallback((id: string, name: string, vector: number[]) => {
    setEntries((prev) => [...prev, { id, name, vector }]);
  }, []);

  const findDuplicate = useCallback((vector: number[]) => bestMatch(vector, entries, DUPLICATE_THRESHOLD), [entries]);
  const match = useCallback((vector: number[]) => bestMatch(vector, entries), [entries]);
  const clear = useCallback(() => setEntries([]), []);

  const value = useMemo<FaceGalleryValue>(
    () => ({
      entries,
      personCount: new Set(entries.map((e) => e.id)).size,
      register,
      findDuplicate,
      match,
      clear,
    }),
    [entries, register, findDuplicate, match, clear],
  );

  return <FaceGalleryContext.Provider value={value}>{children}</FaceGalleryContext.Provider>;
}

export function useFaceGallery(): FaceGalleryValue {
  const ctx = useContext(FaceGalleryContext);
  if (!ctx) throw new Error('useFaceGallery must be used within a FaceGalleryProvider');
  return ctx;
}
