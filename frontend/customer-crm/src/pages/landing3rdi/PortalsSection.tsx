import type { ReactNode } from "react";
import { Icon } from "./Icon";
import { useReveal } from "./useReveal";
import { useTiltGlare } from "./useTiltGlare";
import { LANDING_COLORS, LANDING_FONT_MONO } from "./tokens";

interface Portal {
  href: string;
  icon: string;
  iconColor: string;
  iconBg: string;
  iconBorder: string;
  badge: ReactNode;
  highlighted?: boolean;
  title: string;
  description: string;
  cta: string;
  ctaIcon: string;
  ctaBg: string;
  ctaFg: string;
  ctaBorder?: string;
}

const PORTALS: Portal[] = [
  {
    href: "https://edge.3rdi.in/",
    icon: "memory",
    iconColor: LANDING_COLORS.violet,
    iconBg: "rgba(167,139,250,.18)",
    iconBorder: "rgba(167,139,250,.35)",
    badge: (
      <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 10, fontWeight: 700, letterSpacing: ".1em", padding: "3px 8px", borderRadius: 4, background: LANDING_COLORS.violet, color: LANDING_COLORS.violetInk }}>
        CRITICAL ACCESS
      </span>
    ),
    highlighted: true,
    title: "Edge Portal",
    description: "Fleet management, zero-touch provisioning, firmware OTA, and local node diagnostics across Jetson & industrial clusters.",
    cta: "Launch Edge Portal",
    ctaIcon: "open_in_new",
    ctaBg: LANDING_COLORS.violet,
    ctaFg: LANDING_COLORS.violetInk,
  },
  {
    href: "https://demo.3rdi.in/",
    icon: "center_focus_strong",
    iconColor: LANDING_COLORS.emerald,
    iconBg: "#1e1e22",
    iconBorder: LANDING_COLORS.border,
    badge: <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 10, letterSpacing: ".1em", color: LANDING_COLORS.emerald }}>INTERACTIVE DEMO</span>,
    title: "Live AI Demo",
    description: "Explore computer vision models with real-time video stream testing, bounding box latency benchmarks, and pipeline builder.",
    cta: "Open Testbed Sandbox",
    ctaIcon: "open_in_new",
    ctaBg: "#1e1e22",
    ctaFg: LANDING_COLORS.ink,
    ctaBorder: LANDING_COLORS.border,
  },
  {
    href: "https://console.3rdi.in/login",
    icon: "terminal",
    iconColor: LANDING_COLORS.violetLight,
    iconBg: "#1e1e22",
    iconBorder: LANDING_COLORS.border,
    badge: <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 10, letterSpacing: ".1em", color: LANDING_COLORS.textMuted }}>SDK &amp; gRPC</span>,
    title: "Developer Console",
    description: "API keys, webhook dispatchers, RTSP ingest pipelines, gRPC streams, and model retraining workflows with telemetry analytics.",
    cta: "Sign In Console",
    ctaIcon: "north_east",
    ctaBg: "#1e1e22",
    ctaFg: LANDING_COLORS.textMuted,
    ctaBorder: LANDING_COLORS.border,
  },
  {
    href: "https://app.3rdi.in/login",
    icon: "dashboard_customize",
    iconColor: LANDING_COLORS.ink,
    iconBg: "#1e1e22",
    iconBorder: LANDING_COLORS.border,
    badge: <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 10, letterSpacing: ".1em", color: LANDING_COLORS.textMuted }}>ENTERPRISE</span>,
    title: "Customer CRM",
    description: "Enterprise operations dashboard, incident management, automated dispatch alerts, and multi-facility compliance reports.",
    cta: "Access Dashboard",
    ctaIcon: "north_east",
    ctaBg: "#1e1e22",
    ctaFg: LANDING_COLORS.textMuted,
    ctaBorder: LANDING_COLORS.border,
  },
];

function PortalCard({ portal, delay }: { portal: Portal; delay: number }) {
  const { ref, style } = useReveal(delay);
  const { cardRef, glareRef, onPointerMove, onPointerLeave } = useTiltGlare();

  return (
    <div ref={ref} style={style}>
      <div
        ref={cardRef}
        onPointerMove={onPointerMove}
        onPointerLeave={onPointerLeave}
        style={{
          position: "relative",
          height: "100%",
          boxSizing: "border-box",
          padding: 28,
          borderRadius: 20,
          background: LANDING_COLORS.surface,
          backdropFilter: "blur(12px)",
          border: `1px solid ${portal.highlighted ? "rgba(167,139,250,.5)" : LANDING_COLORS.border}`,
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          gap: 36,
          boxShadow: portal.highlighted ? "0 30px 80px -30px rgba(124,58,237,.55)" : undefined,
          transformStyle: "preserve-3d",
          transition: "transform .4s cubic-bezier(.2,.7,.2,1), border-color .3s",
        }}
      >
        <div ref={glareRef} style={{ position: "absolute", inset: 0, borderRadius: 20, pointerEvents: "none", opacity: 0, transition: "opacity .3s" }} />
        <div style={{ transform: "translateZ(50px)" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 22 }}>
            <span style={{ width: 52, height: 52, borderRadius: 14, background: portal.iconBg, border: `1px solid ${portal.iconBorder}`, display: "grid", placeItems: "center", color: portal.iconColor }}>
              <Icon name={portal.icon} size={26} />
            </span>
            {portal.badge}
          </div>
          <h3 style={{ margin: 0, fontSize: 20, fontWeight: 700, letterSpacing: "-0.02em" }}>{portal.title}</h3>
          <p style={{ margin: "10px 0 0", fontSize: 14, lineHeight: 1.6, color: LANDING_COLORS.textMuted }}>{portal.description}</p>
        </div>
        <a
          href={portal.href}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            transform: "translateZ(30px)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "12px 16px",
            borderRadius: 10,
            fontSize: 13,
            fontWeight: portal.highlighted ? 700 : 600,
            color: portal.ctaFg,
            background: portal.ctaBg,
            border: portal.ctaBorder ? `1px solid ${portal.ctaBorder}` : undefined,
            boxShadow: portal.highlighted ? "0 0 20px rgba(167,139,250,.4)" : undefined,
          }}
        >
          {portal.cta}
          <Icon name={portal.ctaIcon} size={16} />
        </a>
      </div>
    </div>
  );
}

/** The 4 SSO entry points (Edge Portal / Live Demo / Developer Console / Customer CRM),
 *  each a pointer-tilted card - see useTiltGlare.ts for the shared tilt mechanism. */
export function PortalsSection() {
  const heading = useReveal(0);

  return (
    <section id="portals" data-scene="1" style={{ position: "relative", zIndex: 1, padding: "clamp(100px,14vh,160px) clamp(20px,4vw,48px)" }}>
      <div style={{ maxWidth: 1240, margin: "0 auto" }}>
        <div ref={heading.ref} style={{ ...heading.style, display: "flex", flexWrap: "wrap", alignItems: "flex-end", justifyContent: "space-between", gap: 24, marginBottom: 56 }}>
          <div style={{ maxWidth: 640 }}>
            <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 12, letterSpacing: ".2em", color: LANDING_COLORS.violet }}>SSO INFRASTRUCTURE</span>
            <h2 style={{ margin: "14px 0 0", fontSize: "clamp(36px,5vw,64px)", fontWeight: 800, letterSpacing: "-0.04em", lineHeight: 1 }}>Unified platform entry points</h2>
            <p style={{ margin: "16px 0 0", fontSize: 16, lineHeight: 1.6, color: LANDING_COLORS.textMuted }}>
              Direct single sign-on access to all 3RDI operations, edge nodes, developer toolchains, and interactive testbeds.
            </p>
          </div>
          <span style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: LANDING_FONT_MONO, fontSize: 12, color: LANDING_COLORS.emerald }}>
            <span className="r3-pulse" style={{ width: 7, height: 7, borderRadius: "50%", background: LANDING_COLORS.emerald }} />
            Zero-Trust Gateway Active
          </span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(250px,1fr))", gap: 20 }}>
          {PORTALS.map((portal, i) => (
            <PortalCard key={portal.title} portal={portal} delay={i * 120} />
          ))}
        </div>
      </div>
    </section>
  );
}
