/** Shared constants for the 3RDI CSense marketing landing page
 *  (design/3RDI CSense.dc.html handoff). This page is deliberately its own visual
 *  universe - Geist/Geist Mono/dark palette - distinct from the rest of this app's
 *  Space Grotesk/"Technical Atmosphere" theme (see Landing3rdiPage.tsx's own docstring),
 *  so these are not merged into src/styles.css's token system.
 */
export const LANDING_COLORS = {
  bg: "#09090b",
  surface: "rgba(18,18,21,.82)",
  surfaceStrong: "rgba(18,18,21,.88)",
  border: "#27272a",
  borderStrong: "#52525b",
  ink: "#fafafa",
  textMuted: "#a1a1aa",
  textFaint: "#71717a",
  violet: "#a78bfa",
  violetLight: "#c4b5fd",
  violetDeep: "#7c3aed",
  violetInk: "#0a0012",
  emerald: "#34d399",
  danger: "#ef4444",
} as const;

export const LANDING_FONT_SANS = "'Geist',system-ui,sans-serif";
export const LANDING_FONT_MONO = "'Geist Mono',monospace";
export const LANDING_FONT_ICON = "'Material Symbols Outlined'";

export const LANDING_EASE = "cubic-bezier(.2,.7,.2,1)";

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
