/**
 * The exact same palette as frontend/customer-crm/src/styles.css's `:root` block - copied
 * hex-for-hex rather than re-derived, so a severity/status colour means the same thing on
 * the web CRM and on this app. "Technical Atmosphere" (design/csense-ui/System.dc.html) -
 * one theme, deliberately, same as the web app: this palette is the product's own
 * identity, specified by the owner, not a dark-mode variant of a light default. There is
 * therefore no more `light`/`dark` split here, matching the web CRM's own removal of its
 * `prefers-color-scheme` branch - `useColors()` keeps its existing call signature (no
 * screen has to change how it reads the palette) but no longer branches on the OS scheme.
 */
const palette = {
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

/** Kept as a hook (rather than a plain export) so every existing call site
 * (`const colors = useColors()`) needed zero changes when this stopped branching on the
 * OS colour scheme - only this function's own body changed. */
export function useColors(): Palette {
  return palette;
}
