/**
 * Reskinned from "Technical Atmosphere" per the Claude Design handoff bundle
 * (`CSense Mobile App Reskin.dc.html`), mirroring the web CRM's own reskin
 * (frontend/customer-crm/src/styles.css) so a severity/status colour still means the same
 * thing on the web CRM and on this app - same token roles, values adapted to this app's
 * own screen-background convention (a single screen surface rather than the web's
 * canvas-behind-card nesting). One theme, deliberately, same as the web app: this palette
 * is the product's own identity, specified by the owner, not a dark-mode variant of a
 * light default. There is therefore no `light`/`dark` split here, matching the web CRM's
 * own removal of its `prefers-color-scheme` branch - `useColors()` keeps its existing call
 * signature (no screen has to change how it reads the palette) but no longer branches on
 * the OS scheme.
 */
const palette = {
  canvas: '#1a1418',
  well: '#1d1016',
  raised: '#120d10',
  hair: '#362a31',
  hairStrong: '#4a3b43',
  emerald: '#22e3a0',
  rose: '#ff1f5c',
  crimson: '#7a0b30',
  crimsonDeep: '#3a0a1c',
  violetGlow: '#6d28d7',
  ink: '#f5edf0',
  dim: '#a2929a',
  faint: '#6e5f67',

  surface: '#1d1016',
  surfaceSunken: '#120d10',
  border: '#362a31',
  text: '#f5edf0',
  textMuted: '#a2929a',
  textInverse: '#fff0f6',
  accent: '#ff1f5c',
  accentHover: '#ff6b93',
  accentSubtle: 'rgba(255, 31, 92, 0.12)',

  critical: '#ff9db8',
  criticalSubtle: 'rgba(255, 45, 94, 0.14)',
  high: '#ff9d5c',
  highSubtle: 'rgba(255, 157, 92, 0.14)',
  medium: '#e8c072',
  mediumSubtle: 'rgba(232, 192, 114, 0.14)',
  low: '#22e3a0',
  lowSubtle: 'rgba(34, 227, 160, 0.14)',
  info: '#a2929a',
  infoSubtle: '#120d10',
};

export type Palette = typeof palette;

/** Kept as a hook (rather than a plain export) so every existing call site
 * (`const colors = useColors()`) needed zero changes when this stopped branching on the
 * OS colour scheme - only this function's own body changed. */
export function useColors(): Palette {
  return palette;
}
