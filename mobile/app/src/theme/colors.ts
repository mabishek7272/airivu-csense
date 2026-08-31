import { useColorScheme } from 'react-native';

/**
 * The exact same palette as frontend/customer-crm/src/styles.css's `:root` /
 * `prefers-color-scheme: dark` blocks - copied hex-for-hex rather than re-derived, so a
 * severity/status colour means the same thing on the web CRM and on this app.
 */
const light = {
  surface: '#ffffff',
  surfaceSunken: '#eef0f3',
  border: '#d5d9e0',
  text: '#14181f',
  textMuted: '#545c6a',
  accent: '#1f5fd0',
  accentSubtle: '#e8effb',
  critical: '#a5102a',
  criticalSubtle: '#fdeaee',
  high: '#b4470a',
  highSubtle: '#fdf0e6',
  medium: '#8a6100',
  mediumSubtle: '#fbf3e0',
  low: '#1c6b52',
  lowSubtle: '#e6f4ef',
  info: '#35506e',
  infoSubtle: '#eaf0f7',
};

const dark = {
  surface: '#1a1e25',
  surfaceSunken: '#23282f',
  border: '#333a44',
  text: '#eef1f5',
  textMuted: '#a3acba',
  accent: '#6ea3f5',
  accentSubtle: '#1c2939',
  critical: '#ff8fa1',
  criticalSubtle: '#33161c',
  high: '#f7ad72',
  highSubtle: '#331f11',
  medium: '#e5c268',
  mediumSubtle: '#2e2612',
  low: '#7ddcb8',
  lowSubtle: '#122b23',
  info: '#a8c3e0',
  infoSubtle: '#1a2431',
};

export type Palette = typeof light;

/** `useColorScheme()` follows the OS setting, same as the web app's own
 * `prefers-color-scheme` media query - no separate in-app theme toggle to keep in sync. */
export function useColors(): Palette {
  const scheme = useColorScheme();
  return scheme === 'dark' ? dark : light;
}
