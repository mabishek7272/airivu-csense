import { createContext, useContext } from 'react';

/**
 * The exact same palette as frontend/customer-crm/src/styles.css's `:root` block - copied
 * hex-for-hex rather than re-derived, so a severity/status colour means the same thing on
 * the web CRM and on this app. "Technical Atmosphere" (design/csense-ui/System.dc.html) -
 * still the identity for eAIgleye/3RDi (both placeholder-asset brands today), and not an
 * OS-`prefers-color-scheme` toggle for any brand - `useColors()` never branches on the OS
 * scheme, only on which brand a build is (see `BRAND_PALETTE` below). CSense itself moved
 * off this palette on 2026-09-28 onto its own light `csensePalette`.
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
 * one - for Apti (chosen 2026-09-27). Every field mirrors the dark palette's own role
 * exactly (same names, same consumers - no screen needs to know which base is active),
 * but severity/status colours (`critical`/`high`/`medium`/`low` and their `*Subtle`
 * tints) are recalibrated, not just copied: the dark palette's own values (e.g.
 * `medium: '#e0be72'`, a light gold) are near-invisible against a light canvas, so a
 * light theme needs its own darker, still-legible set for the same semantic meaning,
 * not the same hex values reused on the wrong background. */
export const aptiPalette: Palette = {
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

/** CSense's own light palette - "Majestic White & Cherry Purple" (2026-09-28),
 * replacing the dark "Technical Atmosphere" palette above as CSense's identity. A
 * genuinely different palette from Apti's (different brand, different colours), not a
 * shared "the" light theme - which is why this file now keys a base palette per brand
 * (`BRAND_PALETTE` below) rather than picking between exactly two options. Severity
 * colours are recalibrated the same way Apti's were, for the same reason: the dark
 * palette's own light-on-dark values don't carry over to a light canvas. */
export const csensePalette: Palette = {
  canvas: '#f8f7fb',
  well: '#f1eef7',
  raised: '#ffffff',
  hair: '#e3e1ea',
  hairStrong: '#cbc7d6',
  emerald: '#24a148',
  rose: '#7b2cbf',
  crimson: '#5a189a',
  ink: '#2b2b35',
  dim: '#6c6c7a',
  faint: '#a8a6b3',

  surface: '#f8f7fb',
  surfaceSunken: '#f1eef7',
  border: '#d9d9e3',
  text: '#2b2b35',
  textMuted: '#6c6c7a',
  textInverse: '#ffffff',
  accent: '#7b2cbf',
  // Darker on press/hover, not brighter - correct for a light theme (the dark
  // palette's own accentHover goes brighter, which would read as backwards here).
  accentHover: '#6b21a8',
  accentSubtle: '#eadcf8',

  critical: '#d7263d',
  criticalSubtle: 'rgba(215, 38, 61, 0.12)',
  // The design brief this palette is drawn from names only 4 alert colours for what is
  // actually a 5-level severity system - `high` has no direct source colour, so this is
  // derived (a burnt orange between the brief's red and amber) to keep all 5 severities
  // visually distinct rather than reusing `critical` or `medium`'s hue.
  high: '#e8590c',
  highSubtle: 'rgba(232, 89, 12, 0.14)',
  medium: '#f4a300',
  mediumSubtle: 'rgba(244, 163, 0, 0.16)',
  low: '#24a148',
  lowSubtle: 'rgba(36, 161, 72, 0.12)',
  info: '#3a86ff',
  infoSubtle: 'rgba(58, 134, 255, 0.12)',
};

/** Which base palette a build-time brand variant starts from, keyed by
 * `app.config.ts`'s own `extra.brandSlug` (and `BrandProvider.tsx`'s pre-login
 * fetch, which reads that same slug). Not derived from anything in `org_branding` -
 * a reseller's branding row supplies accent/button colours (see `BrandProvider.tsx`'s
 * `withOverrides`), not which base palette a brand starts from, which is a per-brand
 * product decision made once, here. A direct per-brand map, not a `dark`/`light` mode
 * switch - with both Apti and CSense now on their own distinct light palettes, "light
 * mode" no longer names a single palette. */
export const BRAND_PALETTE: Record<string, Palette> = {
  apti: aptiPalette,
  csense: csensePalette,
  eaigleye: palette,
  '3rdi': palette,
};

/** Whether a brand's real `org_branding.color_canvas` is allowed to override its base
 * palette's background fields (`canvas`/`surface`/`surfaceSunken`). Every branding row
 * that has ever been configured used a dark tone (it predates any light-themed brand
 * existing) - applying it to a light-palette brand would silently turn its app dark
 * again the moment branding resolves. `false` for every brand on a light base palette,
 * `true` for brands still on the shared dark palette. */
export const BRAND_ALLOW_CANVAS_OVERRIDE: Record<string, boolean> = {
  apti: false,
  csense: false,
  eaigleye: true,
  '3rdi': true,
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
  displayName: '',
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
