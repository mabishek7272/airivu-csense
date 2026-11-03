import { CountUp } from "./CountUp";
import { Icon } from "./Icon";
import { useReveal } from "./useReveal";
import { useTiltGlare } from "./useTiltGlare";
import { LANDING_COLORS, LANDING_FONT_MONO } from "./tokens";

interface Feature {
  icon: string;
  iconColor: string;
  iconBg: string;
  iconBorder: string;
  title: string;
  description: string;
}

const FEATURES: Feature[] = [
  {
    icon: "lock_person",
    iconColor: LANDING_COLORS.emerald,
    iconBg: "rgba(52,211,153,.15)",
    iconBorder: "rgba(52,211,153,.3)",
    title: "Data sovereignty & zero-egress",
    description: "Video never leaves your physical perimeter. Inference runs on-premise, transferring only zero-knowledge metadata.",
  },
  {
    icon: "speed",
    iconColor: LANDING_COLORS.violet,
    iconBg: "rgba(167,139,250,.15)",
    iconBorder: "rgba(167,139,250,.3)",
    title: "Sub-20ms real-time SLA",
    description: "Pipelining from camera sensor to automated dispatch, preventing incidents before they unfold.",
  },
  {
    icon: "lan",
    iconColor: LANDING_COLORS.violetLight,
    iconBg: "rgba(196,181,253,.15)",
    iconBorder: "rgba(196,181,253,.3)",
    title: "Self-healing edge clusters",
    description: "Dynamic node failover keeps coverage continuous through WAN loss or local hardware disruption.",
  },
  {
    icon: "verified_user",
    iconColor: LANDING_COLORS.emerald,
    iconBg: "rgba(52,211,153,.15)",
    iconBorder: "rgba(52,211,153,.3)",
    title: "Compliance verified",
    description: "SOC 2 Type II, ISO/IEC 27001, HIPAA-compliant masking, and GDPR/DPDP biometric storage standards.",
  },
];

function FeatureCard({ feature, delay }: { feature: Feature; delay: number }) {
  const { ref, style } = useReveal(delay);
  const { cardRef, glareRef, onPointerMove, onPointerLeave } = useTiltGlare();
  return (
    <div ref={ref} style={style}>
      <div
        ref={cardRef}
        onPointerMove={onPointerMove}
        onPointerLeave={onPointerLeave}
        style={{ position: "relative", height: "100%", boxSizing: "border-box", padding: 26, borderRadius: 20, background: LANDING_COLORS.surface, backdropFilter: "blur(12px)", border: `1px solid ${LANDING_COLORS.border}`, transformStyle: "preserve-3d", transition: "transform .4s cubic-bezier(.2,.7,.2,1), border-color .3s" }}
      >
        <div ref={glareRef} style={{ position: "absolute", inset: 0, borderRadius: 20, pointerEvents: "none", opacity: 0, transition: "opacity .3s" }} />
        <span style={{ transform: "translateZ(40px)", width: 46, height: 46, borderRadius: 12, background: feature.iconBg, border: `1px solid ${feature.iconBorder}`, display: "grid", placeItems: "center", color: feature.iconColor }}>
          <Icon name={feature.icon} size={24} />
        </span>
        <h4 style={{ margin: "20px 0 0", fontSize: 18, fontWeight: 700, letterSpacing: "-0.02em", transform: "translateZ(25px)" }}>{feature.title}</h4>
        <p style={{ margin: "10px 0 0", fontSize: 14, lineHeight: 1.6, color: LANDING_COLORS.textMuted }}>{feature.description}</p>
      </div>
    </div>
  );
}

/** "Engineered for places where downtime isn't an option": compliance/reliability feature
 *  grid plus animated stat counters (see CountUp.tsx). Mockup content ships as written
 *  (SOC 2 Type II, ISO/IEC 27001, HIPAA, GDPR/DPDP; 99.995% uptime, 2.4M+ streams/day) -
 *  per explicit confirmation, accuracy of these specific claims is verified separately
 *  before this page goes publicly live, not something to soften or hedge in the markup. */
export function SecuritySection() {
  const heading = useReveal(0);
  const stats = useReveal(0);

  return (
    <section id="security" data-scene="5" style={{ position: "relative", zIndex: 1, padding: "clamp(100px,14vh,160px) clamp(20px,4vw,48px)" }}>
      <div style={{ maxWidth: 1240, margin: "0 auto" }}>
        <div ref={heading.ref} style={{ ...heading.style, maxWidth: 760, marginBottom: 56 }}>
          <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 12, letterSpacing: ".2em", color: LANDING_COLORS.violet }}>SECURITY &amp; RELIABILITY</span>
          <h2 style={{ margin: "14px 0 0", fontSize: "clamp(36px,5vw,64px)", fontWeight: 800, letterSpacing: "-0.04em", lineHeight: 1 }}>
            Engineered for places where downtime isn&apos;t an option
          </h2>
          <p style={{ margin: "16px 0 0", fontSize: 16, lineHeight: 1.6, color: LANDING_COLORS.textMuted }}>
            Mission-critical industrial, transportation, and governmental installations where video privacy and uptime are non-negotiable.
          </p>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))", gap: 20 }}>
          {FEATURES.map((feature, i) => (
            <FeatureCard key={feature.title} feature={feature} delay={i * 120} />
          ))}
        </div>

        <div
          ref={stats.ref}
          style={{ ...stats.style, marginTop: 28, padding: "36px 28px", borderRadius: 22, background: "rgba(24,24,27,.85)", backdropFilter: "blur(12px)", border: `1px solid ${LANDING_COLORS.border}`, display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(170px,1fr))", gap: 28, textAlign: "center" }}
        >
          <div>
            <CountUp to={99.995} decimals={3} suffix="%" style={{ fontSize: "clamp(34px,4vw,52px)", fontWeight: 800, letterSpacing: "-0.04em" }} />
            <div style={{ marginTop: 6, fontSize: 13, color: LANDING_COLORS.textMuted }}>Cluster availability</div>
          </div>
          <div>
            <CountUp to={12} decimals={0} prefix="< " suffix="ms" style={{ fontSize: "clamp(34px,4vw,52px)", fontWeight: 800, letterSpacing: "-0.04em", color: LANDING_COLORS.emerald }} />
            <div style={{ marginTop: 6, fontSize: 13, color: LANDING_COLORS.textMuted }}>Median edge latency</div>
          </div>
          <div>
            <CountUp to={2.4} decimals={1} suffix="M+" style={{ fontSize: "clamp(34px,4vw,52px)", fontWeight: 800, letterSpacing: "-0.04em" }} />
            <div style={{ marginTop: 6, fontSize: 13, color: LANDING_COLORS.textMuted }}>Processed streams / day</div>
          </div>
          <div>
            <div style={{ fontSize: "clamp(34px,4vw,52px)", fontWeight: 800, letterSpacing: "-0.04em", color: LANDING_COLORS.violet }}>Zero</div>
            <div style={{ marginTop: 6, fontSize: 13, color: LANDING_COLORS.textMuted }}>Cloud egress dependencies</div>
          </div>
        </div>
      </div>
    </section>
  );
}
