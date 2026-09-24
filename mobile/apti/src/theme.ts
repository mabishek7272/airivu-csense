/**
 * Nocturne design system tokens, ported from the approved design handoff
 * (the `_ds/nocturne-.../styles.css` handoff file). Values are copied 1:1, not eyeballed — this file is the
 * source of truth for the app's look, mirroring the CSS file's own stated role.
 *
 * px values in the source CSS were generated for a desktop deck's own base unit; the
 * `space-*` scale here keeps the CSS's exact numbers since the mockup's own phone
 * artboard already uses them directly (e.g. `padding: var(--space-4) 16px 40px`).
 */

export const color = {
  bg: '#161826',
  surface: '#232532',
  text: '#e9e9ed',
  accent: '#9184d9',
  accent2: '#a7a1db',
  // color-mix(in srgb, #e9e9ed 16%, transparent) flattened onto `bg` for RN (no color-mix).
  divider: 'rgba(233,233,237,0.16)',

  neutral100: '#f3f5fe',
  neutral200: '#e4e7f5',
  neutral300: '#cfd3e5',
  neutral400: '#b2b6ca',
  neutral500: '#9397ab',
  neutral600: '#75798c',
  neutral700: '#595d6c',
  neutral800: '#3f424d',
  neutral900: '#292b31',

  accent100: '#f5f4ff',
  accent200: '#e7e5fe',
  accent300: '#d2cefd',
  accent400: '#b5abfc',
  accent500: '#968ae0',
  accent600: '#796cbf',
  accent700: '#5d5294',
  accent800: '#423a6a',
  accent900: '#2b2741',
} as const;

// text at 50/55/60/70% opacity, flattened for RN (color-mix has no RN equivalent).
export const textMuted = 'rgba(233,233,237,0.55)';
export const textMuted50 = 'rgba(233,233,237,0.5)';
export const textMuted70 = 'rgba(233,233,237,0.7)';

export const space = {
  1: 2.8,
  2: 5.6,
  3: 8.4,
  4: 11.2,
  6: 16.8,
  8: 22.4,
} as const;

export const radius = {
  sm: 4,
  md: 8,
  lg: 14,
} as const;

// RN has no multi-layer box-shadow; `.elev-*`'s hairline ring is approximated with a
// border (drawn in the component), and only the ambient blur travels through these.
export const shadow = {
  sm: { shadowColor: '#000', shadowOpacity: 0, shadowRadius: 0, elevation: 0 },
  md: {
    shadowColor: '#000', shadowOffset: { width: 0, height: 6 }, shadowOpacity: 0.55,
    shadowRadius: 18, elevation: 6,
  },
  lg: {
    shadowColor: '#000', shadowOffset: { width: 0, height: 16 }, shadowOpacity: 0.65,
    shadowRadius: 40, elevation: 12,
  },
} as const;

export const fontHeading = 'Inter_500Medium';
export const fontHeadingSemibold = 'Inter_600SemiBold';
export const fontBody = 'Inter_400Regular';
