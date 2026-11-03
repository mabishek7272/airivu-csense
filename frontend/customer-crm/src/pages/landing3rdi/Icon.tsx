import { LANDING_FONT_ICON } from "./tokens";

/** One ligature from the Material Symbols Outlined font (e.g. "memory", "terminal") -
 *  loaded scoped to this page only, see Landing3rdiPage.tsx's font injection. */
export function Icon({ name, size = 18, color }: { name: string; size?: number; color?: string }) {
  return (
    <span
      style={{
        fontFamily: LANDING_FONT_ICON,
        fontSize: size,
        lineHeight: 1,
        color,
        display: "inline-block",
      }}
    >
      {name}
    </span>
  );
}
