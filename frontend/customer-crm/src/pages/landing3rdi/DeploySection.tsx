import { useEffect, useRef } from "react";
import { Icon } from "./Icon";
import { useReveal } from "./useReveal";
import { LANDING_COLORS, LANDING_FONT_MONO, prefersReducedMotion } from "./tokens";

const CHECKS = ["Instant RTSP ingestion", "ONNX & TensorRT native", "Zero hardware lock-in"];

/** Closing CTA band: two oversized outline-text marquees drifting opposite directions as
 *  the reader scrolls past, ported from the mockup's `data-marquee` transform in `tick()`. */
export function DeploySection() {
  const content = useReveal(0);
  const marqueeRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    if (prefersReducedMotion()) return;
    let raf = 0;
    const onScroll = () => {
      raf = requestAnimationFrame(() => {
        const vh = window.innerHeight;
        marqueeRefs.current.forEach((el) => {
          if (!el) return;
          const dir = Number(el.dataset.dir);
          const rect = el.getBoundingClientRect();
          el.style.transform = `translate3d(${(rect.top - vh) * 0.45 * dir}px,0,0)`;
        });
      });
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", onScroll);
    };
  }, []);

  return (
    <section id="deploy" data-scene="6" style={{ position: "relative", zIndex: 1, padding: "clamp(80px,12vh,140px) 0 clamp(100px,16vh,180px)", overflow: "hidden" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 64, pointerEvents: "none" }}>
        <div
          ref={(el) => (marqueeRefs.current[0] = el)}
          data-dir="-1"
          style={{ whiteSpace: "nowrap", fontSize: "clamp(72px,13vw,210px)", fontWeight: 800, letterSpacing: "-0.05em", lineHeight: 1, color: "transparent", WebkitTextStroke: "1px rgba(167,139,250,.4)", willChange: "transform" }}
        >
          INTELLIGENT VISION · CONNECTED INFRASTRUCTURE · INTELLIGENT VISION ·
        </div>
        <div
          ref={(el) => (marqueeRefs.current[1] = el)}
          data-dir="1"
          style={{ whiteSpace: "nowrap", fontSize: "clamp(72px,13vw,210px)", fontWeight: 800, letterSpacing: "-0.05em", lineHeight: 1, color: "transparent", WebkitTextStroke: "1px rgba(52,211,153,.32)", willChange: "transform", marginLeft: "-60vw" }}
        >
          REAL-TIME DECISIONS · EDGE AI · REAL-TIME DECISIONS · EDGE AI ·
        </div>
      </div>

      <div ref={content.ref} style={{ ...content.style, maxWidth: 900, margin: "0 auto", padding: "0 clamp(20px,4vw,48px)", textAlign: "center" }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "6px 12px", borderRadius: 999, background: LANDING_COLORS.surfaceStrong, border: `1px solid ${LANDING_COLORS.border}`, fontFamily: LANDING_FONT_MONO, fontSize: 11, letterSpacing: ".12em", color: LANDING_COLORS.violet }}>
          <Icon name="rocket_launch" size={15} />
          READY FOR HIGH-THROUGHPUT DEPLOYMENT
        </span>
        <h2 style={{ margin: "24px 0 0", fontSize: "clamp(40px,6vw,84px)", fontWeight: 800, letterSpacing: "-0.05em", lineHeight: 0.95, textWrap: "balance" }}>
          Deploy intelligent vision across your infrastructure
        </h2>
        <p style={{ margin: "22px auto 0", fontSize: 17, lineHeight: 1.6, color: LANDING_COLORS.textMuted, maxWidth: 600 }}>
          Connect your cameras to 3RDI CSense in minutes. Provision edge nodes, test neural vision pipelines, or launch the interactive sandbox.
        </p>
        <div style={{ marginTop: 40, display: "flex", flexWrap: "wrap", gap: 14, justifyContent: "center" }}>
          <a
            href="https://edge.3rdi.in/"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-cta-primary"
            style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "17px 28px", borderRadius: 12, fontSize: 15, fontWeight: 700, color: LANDING_COLORS.violetInk, background: LANDING_COLORS.violet, boxShadow: "0 0 40px rgba(167,139,250,.55)", border: "1px solid rgba(196,181,253,.4)" }}
          >
            <Icon name="memory" size={20} />
            Launch Edge Portal
            <Icon name="arrow_forward" size={18} />
          </a>
          <a
            href="https://demo.3rdi.in/"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-cta-secondary"
            style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "17px 24px", borderRadius: 12, fontSize: 15, fontWeight: 600, color: LANDING_COLORS.ink, background: "rgba(18,18,21,.88)", border: `1px solid ${LANDING_COLORS.border}` }}
          >
            <Icon name="videocam" size={20} color={LANDING_COLORS.violet} />
            Explore Live AI Demo
          </a>
        </div>
        <div style={{ marginTop: 32, display: "flex", flexWrap: "wrap", gap: 24, justifyContent: "center", fontFamily: LANDING_FONT_MONO, fontSize: 12, color: LANDING_COLORS.textMuted }}>
          {CHECKS.map((check) => (
            <span key={check} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <Icon name="check" size={16} color={LANDING_COLORS.emerald} />
              {check}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}
