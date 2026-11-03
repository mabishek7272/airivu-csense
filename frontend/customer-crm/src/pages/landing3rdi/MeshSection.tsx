import { useEffect, useRef } from "react";
import { useReveal } from "./useReveal";
import { LANDING_COLORS, LANDING_FONT_MONO, prefersReducedMotion } from "./tokens";

interface Step {
  number: string;
  tag: string;
  tagColor: string;
  title: string;
  description: string;
}

const STEPS: Step[] = [
  { number: "01", tag: "SOURCE INGEST", tagColor: LANDING_COLORS.violet, title: "Multi-RTSP feeds", description: "PTZ, thermal, drone and 4K static IP cameras ingested via a zero-copy pipeline." },
  { number: "02", tag: "LOCAL INFERENCE", tagColor: LANDING_COLORS.emerald, title: "Edge vision node", description: "Sub-15ms quantized neural detection, feature extraction, and local alert triggers." },
  { number: "03", tag: "ENCRYPTED MESH", tagColor: LANDING_COLORS.violetLight, title: "Zero-trust WireGuard", description: "Only lightweight JSON metadata and cryptographically signed snapshots egress." },
  { number: "04", tag: "DISPATCH CORE", tagColor: LANDING_COLORS.ink, title: "Enterprise cloud & CRM", description: "Real-time incident response, automated compliance logging, and executive alerting." },
];

/** "From sensor to decision in four hops": a horizontal beam fills left-to-right as the
 *  reader scrolls past, lighting up each step's node in turn - ported from the mockup's
 *  `data-beam`/`data-step`/`data-node` logic in `tick()`. */
export function MeshSection() {
  const heading = useReveal(0);
  const sectionRef = useRef<HTMLDivElement>(null);
  const beamRef = useRef<HTMLDivElement>(null);
  const stepRefs = useRef<(HTMLDivElement | null)[]>([]);
  const stepOn = useRef<boolean[]>([]);

  useEffect(() => {
    if (prefersReducedMotion()) {
      stepRefs.current.forEach((el) => {
        if (!el) return;
        el.style.opacity = "1";
        el.style.transform = "translateY(0)";
      });
      return;
    }
    let raf = 0;
    const onScroll = () => {
      raf = requestAnimationFrame(() => {
        const section = sectionRef.current;
        const beam = beamRef.current;
        if (!section || !beam) return;
        const rect = section.getBoundingClientRect();
        const vh = window.innerHeight;
        const pr = Math.max(0, Math.min(1, (vh * 0.75 - rect.top) / (rect.height * 0.8)));
        beam.style.transform = `scaleX(${pr})`;
        stepRefs.current.forEach((step, i) => {
          if (!step) return;
          const on = pr > (i + 0.3) / stepRefs.current.length;
          if (stepOn.current[i] === on) return;
          stepOn.current[i] = on;
          step.style.opacity = on ? "1" : ".35";
          step.style.transform = on ? "translateY(0)" : "translateY(16px)";
          const node = step.querySelector<HTMLElement>("[data-node]");
          if (node) {
            node.style.borderColor = on ? LANDING_COLORS.violet : "#3f3f46";
            node.style.background = on ? LANDING_COLORS.violet : LANDING_COLORS.bg;
            node.style.boxShadow = on ? `0 0 0 6px rgba(167,139,250,.18), 0 0 24px ${LANDING_COLORS.violet}` : "none";
          }
          const num = step.querySelector<HTMLElement>("[data-step-num]");
          if (num) num.style.color = on ? LANDING_COLORS.violet : LANDING_COLORS.border;
        });
      });
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, []);

  return (
    <section id="mesh" data-scene="3" ref={sectionRef} style={{ position: "relative", zIndex: 1, padding: "clamp(120px,18vh,200px) clamp(20px,4vw,48px)" }}>
      <div style={{ maxWidth: 1240, margin: "0 auto" }}>
        <div ref={heading.ref} style={{ ...heading.style, textAlign: "center", maxWidth: 820, margin: "0 auto 80px" }}>
          <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 12, letterSpacing: ".2em", color: LANDING_COLORS.violet }}>AUTONOMOUS EDGE-TO-CLOUD MESH</span>
          <h2 style={{ margin: "14px 0 0", fontSize: "clamp(38px,5.6vw,76px)", fontWeight: 800, letterSpacing: "-0.045em", lineHeight: 0.98, textWrap: "balance" }}>
            From sensor to decision in four hops
          </h2>
          <p style={{ margin: "18px auto 0", fontSize: 16, lineHeight: 1.6, color: LANDING_COLORS.textMuted, maxWidth: 560 }}>
            Video stays on your perimeter. Only signed metadata travels the mesh.
          </p>
        </div>
        <div style={{ position: "relative" }}>
          <div style={{ position: "absolute", left: 0, right: 0, top: 9, height: 2, background: LANDING_COLORS.border, borderRadius: 2 }}>
            <div ref={beamRef} style={{ height: "100%", background: "linear-gradient(90deg,#7c3aed,#a78bfa,#34d399)", transformOrigin: "0 50%", transform: "scaleX(0)", boxShadow: "0 0 18px rgba(167,139,250,.8)" }} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))", gap: 24 }}>
            {STEPS.map((step, i) => (
              <div
                key={step.number}
                ref={(el) => (stepRefs.current[i] = el)}
                style={{ position: "relative", paddingTop: 44, transition: "transform .6s cubic-bezier(.2,.7,.2,1),opacity .6s", opacity: 0.35 }}
              >
                <span data-node style={{ position: "absolute", top: 0, left: 0, width: 20, height: 20, borderRadius: "50%", background: LANDING_COLORS.bg, border: "2px solid #3f3f46", boxSizing: "border-box", transition: "all .4s" }} />
                <div data-step-num style={{ fontFamily: LANDING_FONT_MONO, fontSize: 56, fontWeight: 600, letterSpacing: "-0.04em", color: LANDING_COLORS.border, lineHeight: 1 }}>{step.number}</div>
                <div style={{ marginTop: 14, fontFamily: LANDING_FONT_MONO, fontSize: 11, letterSpacing: ".16em", color: step.tagColor }}>{step.tag}</div>
                <h3 style={{ margin: "8px 0 0", fontSize: 22, fontWeight: 700, letterSpacing: "-0.02em" }}>{step.title}</h3>
                <p style={{ margin: "10px 0 0", fontSize: 14, lineHeight: 1.6, color: LANDING_COLORS.textMuted }}>{step.description}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
