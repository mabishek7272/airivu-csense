/**
 * Nocturne design tokens - LIGHT variant.
 *
 * The approved handoff (the `_ds/nocturne-.../styles.css` design system) only documents a
 * dark ground ("Nocturne is a quiet, compact dark interface"). Apti asked for a light app
 * instead, and the system's own readme gives the rule for inverting it rather than leaving
 * it to guesswork: each color role carries a 100-900 tonal ramp generated on one shared
 * OKLCH lightness scale, and which step plays which role simply flips with the ground -
 * "dark steps (700-900) for tinted fills... light steps (100-300) for text on those tints"
 * on a dark ground becomes light steps for fills and dark steps for text on a light one.
 * The interaction-state note makes the same point explicitly ("--color-accent-600 on a
 * light ground, --color-accent-400 on a dark one"), and elevation gets its own explicit
 * rule: "soft ink-tinted shadows on a light theme, a hairline edge + ambient darkness on a
 * dark one." The ramp hex values themselves are unchanged from the handoff - only which
 * semantic role points at which step, per that rule.
 */

export const color = {
  bg: '#f3f5fe',
  surface: '#ffffff',
  text: '#20222c',
  // Promoted from accent-700: the handoff's own base accent (#9184d9) is too light to read
  // reliably against a light ground (see the readme's 3:1 contrast note, which is stated
  // for the dark ground and inverts here) - accent-700 is the ramp step this system's own
  // "one step past base, on a light ground" rule points at for real legibility.
  accent: '#5d5294',
  accent2: '#5c5783',
  // was a light-text tint on the dark ground; inverted to a dark-text tint on the light one.
  divider: 'rgba(32,34,44,0.12)',

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

/** Always light, regardless of app theme - for chrome drawn over the camera/photo stage,
 *  which stays a dark viewfinder in both themes (the same convention a phone's own camera
 *  app follows). Never used on the page itself. */
export const stageText = '#e9e9ed';

// text at 50/55/60/70% opacity, flattened for RN (color-mix has no RN equivalent).
export const textMuted = 'rgba(32,34,44,0.6)';
export const textMuted50 = 'rgba(32,34,44,0.55)';
export const textMuted70 = 'rgba(32,34,44,0.75)';

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

// "Soft ink-tinted shadows on a light theme" (the handoff's own words) - gentle, not the
// dark theme's hairline-ring + heavy ambient darkness.
export const shadow = {
  sm: {
    shadowColor: '#000', shadowOffset: { width: 0, height: 1 }, shadowOpacity: 0.05,
    shadowRadius: 3, elevation: 1,
  },
  md: {
    shadowColor: '#000', shadowOffset: { width: 0, height: 4 }, shadowOpacity: 0.08,
    shadowRadius: 14, elevation: 3,
  },
  lg: {
    shadowColor: '#000', shadowOffset: { width: 0, height: 10 }, shadowOpacity: 0.12,
    shadowRadius: 28, elevation: 6,
  },
} as const;

export const fontHeading = 'Inter_500Medium';
export const fontHeadingSemibold = 'Inter_600SemiBold';
export const fontBody = 'Inter_400Regular';
