import { createContext, useContext } from 'react';

/**
 * The exact same palette as frontend/customer-crm/src/styles.css's `:root` block - copied
 * hex-for-hex rather than re-derived, so a severity/status colour means the same thing on
 * the web CRM and on this app. "Technical Atmosphere" (design/csense-ui/System.dc.html) -
 * one theme, deliberately, same as the web app: this palette is the product's own
 * identity, specified by the owner, not a dark-mode variant of a light default. There is
 * therefore no more `light`/`dark` split here, matching the web CRM's own removal of its
 * `prefers-color-scheme` branch - `useColors()` keeps its existing call signature (no
 * screen has to change how it reads the palette) but no longer branches on the OS scheme.
 *
 * Exported (rather than module-private) so `BrandProvider.tsx` can use it as the
 * fallback/base that a tenant's branding colors get merged onto.
 */
export const palette = {
  canvas: '#241c21',
  well: '#1a1418',
  raised: '#2a2026',
  hair: '#362a31',
  hairStrong: '#4a3b43',
  emerald: '#10b981',
  rose: '#ff8abb',
  crimson: '#98134e',
  ink: '#f5edf0',
  dim: '#a2929a',
  faint: '#6e5f67',

  surface: '#1a1418',
  surfaceSunken: '#2a2026',
  border: '#362a31',
  text: '#f5edf0',
  textMuted: '#a2929a',
  textInverse: '#fff0f6',
  accent: '#ff8abb',
  accentHover: '#ffb3d2',
  accentSubtle: 'rgba(255, 138, 187, 0.12)',

  critical: '#ff6b9d',
  criticalSubtle: 'rgba(152, 19, 78, 0.28)',
  high: '#f0a87e',
  highSubtle: 'rgba(194, 96, 58, 0.2)',
  medium: '#e0be72',
  mediumSubtle: 'rgba(138, 106, 46, 0.2)',
  low: '#10b981',
  lowSubtle: 'rgba(16, 185, 129, 0.12)',
  info: '#a2929a',
  infoSubtle: '#2a2026',
};

export type Palette = typeof palette;

/** A second, genuinely different base palette - not a recolored version of the dark
 * one - for brands that should ship as a light theme (Apti, chosen 2026-09-27; the
 * other three brands stay on the dark palette above). Every field mirrors the dark
 * palette's own role exactly (same names, same consumers - no screen needs to know
 * which base is active), but severity/status colours (`critical`/`high`/`medium`/`low`
 * and their `*Subtle` tints) are recalibrated, not just copied: the dark palette's own
 * values (e.g. `medium: '#e0be72'`, a light gold) are near-invisible against a light
 * canvas, so a light theme needs its own darker, still-legible set for the same
 * semantic meaning, not the same hex values reused on the wrong background. */
export const lightPalette: Palette = {
  canvas: '#faf9fc',
  well: '#f1eef5',
  raised: '#ffffff',
  hair: '#e2dee8',
  hairStrong: '#cdc6d6',
  emerald: '#0f9d68',
  rose: '#4a1187',
  crimson: '#8a1a4a',
  ink: '#241c21',
  dim: '#6e5f67',
  faint: '#a2929a',

  surface: '#faf9fc',
  surfaceSunken: '#f1eef5',
  border: '#e2dee8',
  text: '#241c21',
  textMuted: '#6e5f67',
  textInverse: '#ffffff',
  accent: '#4a1187',
  accentHover: '#6b2aa8',
  accentSubtle: 'rgba(74, 17, 135, 0.10)',

  critical: '#c81e4a',
  criticalSubtle: 'rgba(200, 30, 74, 0.12)',
  high: '#b8541f',
  highSubtle: 'rgba(184, 84, 31, 0.14)',
  medium: '#8a6a1e',
  mediumSubtle: 'rgba(138, 106, 30, 0.14)',
  low: '#0f9d68',
  lowSubtle: 'rgba(15, 157, 104, 0.12)',
  info: '#6e5f67',
  infoSubtle: '#f1eef5',
};

/** Which base palette a build-time brand variant starts from, keyed by
 * `app.config.ts`'s own `extra.brandSlug` (and `BrandProvider.tsx`'s pre-login
 * fetch, which reads that same slug). Not derived from anything in `org_branding` -
 * a reseller's branding row supplies accent/button colours (see `BrandProvider.tsx`'s
 * `withOverrides`), not the light-vs-dark choice itself, which is a per-brand product
 * decision made once, here. */
export const BRAND_MODE: Record<string, 'dark' | 'light'> = {
  apti: 'light',
  eaigleye: 'dark',
  '3rdi': 'dark',
  csense: 'dark',
};

/** The shape `BrandProvider` publishes and `useColors`/`useBrand` both read. Defined
 * here (not in `BrandProvider.tsx`) so the dependency between this file and
 * `BrandProvider.tsx` only ever runs one way: `BrandProvider.tsx` imports `palette`,
 * `Palette` and `BrandContext` from `colors.ts`, and `colors.ts` imports nothing back
 * from `BrandProvider.tsx`. Putting the `createContext` call in `BrandProvider.tsx`
 * instead would make that file the one both sides need (`colors.ts` -> `BrandProvider.tsx`
 * for the context, `BrandProvider.tsx` -> `colors.ts` for `palette`/`Palette`) - a real
 * circular import. Keeping the context itself here avoids that entirely. */
export interface BrandState {
  colors: Palette;
  displayName: string;
  logoUrl: string | null;
}

export const BrandContext = createContext<BrandState>({
  colors: palette,
  displayName: 'CSense',
  logoUrl: null,
});

/** Kept as a hook (rather than a plain export) so every existing call site
 * (`const colors = useColors()`) needed zero changes when this stopped branching on the
 * OS colour scheme - only this function's own body changed, first to return the static
 * palette directly, now to read it from `BrandContext` (populated by `BrandProvider`,
 * mounted in `app/_layout.tsx`) so a tenant's branding colors reach every existing call
 * site with zero changes there too. Outside a `BrandProvider` (there should never be such
 * a place in this app, but defensively) this still returns the static fallback palette,
 * since `BrandContext`'s default value above already is `{ colors: palette, ... }`. */
export function useColors(): Palette {
  return useContext(BrandContext).colors;
}
