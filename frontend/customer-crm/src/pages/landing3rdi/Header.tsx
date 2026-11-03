import { Icon } from "./Icon";
import { LANDING_COLORS } from "./tokens";

const NAV_LINKS = [
  { href: "#portals", label: "Platform" },
  { href: "#edge", label: "Edge Hardware" },
  { href: "#mesh", label: "Architecture" },
  { href: "#pipelines", label: "AI Models" },
  { href: "#security", label: "Security" },
];

/** Sticky top nav. Collapses the center nav + "Customer Login" link below 960px
 *  (`narrow`, lifted to Landing3rdiPage since HeroSection reads it too) - the "Edge
 *  Portal" CTA always stays, matching the mockup's own narrow-viewport behavior. */
export function Header({ narrow }: { narrow: boolean }) {
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 50,
        height: 68,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 24,
        padding: "0 clamp(20px,4vw,48px)",
        background: "rgba(9,9,11,.62)",
        backdropFilter: "blur(14px)",
        WebkitBackdropFilter: "blur(14px)",
        borderBottom: `1px solid rgba(39,39,42,.8)`,
      }}
    >
      <a href="#top" style={{ display: "flex", alignItems: "center", gap: 10, color: LANDING_COLORS.ink }}>
        <span
          style={{
            width: 34,
            height: 34,
            borderRadius: 9,
            border: "1px solid #3f3f46",
            background: "#121215",
            display: "grid",
            placeItems: "center",
            color: LANDING_COLORS.violet,
          }}
        >
          <Icon name="visibility" size={20} />
        </span>
        <span style={{ fontWeight: 700, letterSpacing: "-0.02em", fontSize: 16, whiteSpace: "nowrap" }}>
          3RDI <span style={{ color: LANDING_COLORS.violet }}>CSense</span>
        </span>
      </a>

      {!narrow && (
        <nav style={{ display: "flex", gap: 30, fontSize: 13 }}>
          {NAV_LINKS.map((link) => (
            <a key={link.href} href={link.href} className="r3-nav-link">
              {link.label}
            </a>
          ))}
        </nav>
      )}

      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        {!narrow && (
          <a
            href="https://app.3rdi.in/login"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-header-ghost"
            style={{
              fontSize: 13,
              color: LANDING_COLORS.textMuted,
              padding: "9px 14px",
              borderRadius: 9,
              border: `1px solid ${LANDING_COLORS.border}`,
            }}
          >
            Customer Login
          </a>
        )}
        <a
          href="https://edge.3rdi.in/"
          target="_blank"
          rel="noopener noreferrer"
          className="r3-header-cta"
          style={{
            fontSize: 13,
            fontWeight: 700,
            color: LANDING_COLORS.violetInk,
            background: LANDING_COLORS.violet,
            padding: "9px 16px",
            borderRadius: 9,
            boxShadow: "0 0 24px rgba(167,139,250,.4)",
            whiteSpace: "nowrap",
          }}
        >
          Edge Portal
        </a>
      </div>
    </header>
  );
}
