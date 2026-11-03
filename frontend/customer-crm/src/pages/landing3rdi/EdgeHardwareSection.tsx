import { useEffect, useRef } from "react";
import { LANDING_COLORS, LANDING_FONT_MONO, prefersReducedMotion } from "./tokens";

interface HardwareCard {
  tag: string;
  tagColor: string;
  title: string;
  description: string;
  footerLeft: string;
  footerRight: string;
  gradient: string;
}

const CARDS: HardwareCard[] = [
  {
    tag: "275 TOPS INT8 · JETPACK 6.x",
    tagColor: LANDING_COLORS.violet,
    title: "NVIDIA Jetson Orin Series",
    description: "Industrial edge inferencing handling up to 32 concurrent 4K RTSP streams with DeepStream TensorRT optimization.",
    footerLeft: "Latency < 8.2ms",
    footerRight: "Fanless IP50",
    gradient: "radial-gradient(circle at 35% 30%, #203a2e, #0b0c0d 70%)",
  },
  {
    tag: "LOW-POWER ARM · 5W ENVELOPE",
    tagColor: LANDING_COLORS.emerald,
    title: "Raspberry Pi 5 & CM4",
    description: "Cost-effective visual sensors executing quantized MobileNet and YOLO-Nano neural models for auxiliary zones.",
    footerLeft: "Latency < 22.0ms",
    footerRight: "Edge Relay",
    gradient: "radial-gradient(circle at 40% 35%, #1f2a3a, #0b0c0d 70%)",
  },
  {
    tag: "IP67 RATED · HARDWARE TPM 2.0",
    tagColor: LANDING_COLORS.violetLight,
    title: "Spaider Edge Micro-Appliances",
    description: "Hermetically sealed turnkey vision appliances with native neural coprocessors for hostile environments.",
    footerLeft: "Latency < 14.1ms",
    footerRight: "PoE+ Powered",
    gradient: "radial-gradient(circle at 45% 30%, #2a1f3a, #0b0c0d 70%)",
  },
  {
    tag: "MULTI-RACK DGX · ZERO-EGRESS",
    tagColor: LANDING_COLORS.violet,
    title: "On-Premises GPU Clusters",
    description: "Centralized compute for on-premise fine-tuning, historical video retrieval, and high-throughput multi-camera tracking.",
    footerLeft: "Capacity 500+ RTSP",
    footerRight: "Zero Cloud Egress",
    gradient: "radial-gradient(circle at 30% 40%, #1f2630, #0b0c0d 70%)",
  },
];

/** The "hardware-accelerated edge ecosystem" section: a horizontally scroll-jacked card
 *  track inside a tall sticky viewport, ported from the mockup's `data-hscroll`/
 *  `data-htrack`/`data-hcard` logic in `tick()`. Self-contained scroll listener since
 *  nothing else on the page needs this section's scroll progress. */
export function EdgeHardwareSection() {
  const sectionRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const countRef = useRef<HTMLSpanElement>(null);
  const cardRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    if (prefersReducedMotion()) return;
    let raf = 0;
    const onScroll = () => {
      raf = requestAnimationFrame(() => {
        const section = sectionRef.current;
        const track = trackRef.current;
        if (!section || !track) return;
        const vh = window.innerHeight;
        const vw = window.innerWidth;
        const rect = section.getBoundingClientRect();
        const pr = Math.max(0, Math.min(1, -rect.top / Math.max(1, rect.height - vh)));
        const max = Math.max(0, track.scrollWidth - vw);
        track.style.transform = `translate3d(${-pr * max}px,0,0)`;

        let best = 0;
        let bestDist = Infinity;
        cardRefs.current.forEach((card, i) => {
          if (!card) return;
          const cardRect = card.getBoundingClientRect();
          const d = (cardRect.left + cardRect.width / 2 - vw / 2) / vw;
          if (Math.abs(d) < bestDist) {
            bestDist = Math.abs(d);
            best = i;
          }
          card.style.transform = `rotateY(${Math.max(-50, Math.min(50, d * -42))}deg) translateZ(${-Math.abs(d) * 260}px) translateY(${Math.abs(d) * 30}px)`;
          card.style.opacity = String(1 - Math.min(0.65, Math.abs(d) * 0.7));
          card.style.borderColor = Math.abs(d) < 0.18 ? "rgba(167,139,250,.55)" : LANDING_COLORS.border;
        });

        if (barRef.current) barRef.current.style.transform = `scaleX(${pr})`;
        if (countRef.current) countRef.current.textContent = `0${best + 1} / 0${cardRefs.current.length}`;
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
    <section id="edge" data-scene="2" ref={sectionRef} style={{ position: "relative", zIndex: 1, height: "340vh" }}>
      <div style={{ position: "sticky", top: 0, height: "100vh", boxSizing: "border-box", overflow: "hidden", display: "flex", flexDirection: "column", justifyContent: "center", gap: "clamp(14px,3vh,48px)", padding: "clamp(12px,3vh,40px) 0" }}>
        <div style={{ padding: "0 clamp(20px,6vw,96px)", display: "flex", flexWrap: "wrap", alignItems: "flex-end", justifyContent: "space-between", gap: 20, flex: "0 0 auto" }}>
          <div style={{ maxWidth: 680 }}>
            <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: "clamp(10px,1.4vh,12px)", letterSpacing: ".2em", color: LANDING_COLORS.emerald }}>DECENTRALIZED EXECUTION</span>
            <h2 style={{ margin: "clamp(6px,1.4vh,14px) 0 0", fontSize: "clamp(26px,min(4.6vw,6.4vh),60px)", fontWeight: 800, letterSpacing: "-0.04em", lineHeight: 1.02 }}>
              Hardware-accelerated edge ecosystem
            </h2>
            <p style={{ margin: "clamp(6px,1.4vh,14px) 0 0", fontSize: "clamp(12px,1.8vh,16px)", lineHeight: 1.5, color: LANDING_COLORS.textMuted }}>
              Zero-latency distributed visual compute running on-premise — minimal bandwidth, impenetrable privacy.
            </p>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 200 }}>
            <span ref={countRef} style={{ fontFamily: LANDING_FONT_MONO, fontSize: 13, color: LANDING_COLORS.ink, letterSpacing: ".1em" }}>
              01 / 04
            </span>
            <div style={{ height: 2, background: LANDING_COLORS.border, borderRadius: 2, overflow: "hidden" }}>
              <div ref={barRef} style={{ height: "100%", background: "linear-gradient(90deg,#34d399,#a78bfa)", transformOrigin: "0 50%", transform: "scaleX(0)" }} />
            </div>
          </div>
        </div>
        <div style={{ perspective: 1600, flex: "1 1 auto", minHeight: 0, display: "flex", alignItems: "center" }}>
          <div ref={trackRef} style={{ display: "flex", alignItems: "stretch", gap: 36, width: "max-content", height: "min(100%,470px)", padding: "0 clamp(20px,6vw,96px)", willChange: "transform", transformStyle: "preserve-3d" }}>
            {CARDS.map((card, i) => (
              <div
                key={card.title}
                ref={(el) => (cardRefs.current[i] = el)}
                style={{
                  width: "clamp(290px,34vw,460px)",
                  boxSizing: "border-box",
                  padding: "clamp(12px,1.8vh,18px)",
                  borderRadius: 22,
                  background: "rgba(18,18,21,.86)",
                  backdropFilter: "blur(12px)",
                  border: `1px solid ${LANDING_COLORS.border}`,
                  transformStyle: "preserve-3d",
                  willChange: "transform",
                  display: "flex",
                  flexDirection: "column",
                  overflow: "hidden",
                }}
              >
                <div aria-hidden style={{ flex: "1 1 auto", minHeight: 90, borderRadius: 14, overflow: "hidden", background: card.gradient, border: `1px solid ${LANDING_COLORS.border}` }} />
                <div style={{ flex: "0 0 auto", padding: "clamp(10px,2vh,20px) 8px 4px" }}>
                  <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: "clamp(10px,1.4vh,11px)", color: card.tagColor }}>{card.tag}</span>
                  <h3 style={{ margin: "clamp(4px,1vh,8px) 0 0", fontSize: "clamp(16px,2.6vh,22px)", fontWeight: 700, letterSpacing: "-0.02em" }}>{card.title}</h3>
                  <p style={{ margin: "clamp(5px,1.2vh,10px) 0 0", fontSize: "clamp(12px,1.8vh,14px)", lineHeight: 1.5, color: LANDING_COLORS.textMuted }}>{card.description}</p>
                  <div style={{ marginTop: "clamp(8px,1.8vh,18px)", paddingTop: "clamp(7px,1.4vh,14px)", borderTop: `1px solid ${LANDING_COLORS.border}`, display: "flex", justifyContent: "space-between", fontFamily: LANDING_FONT_MONO, fontSize: "clamp(10px,1.5vh,12px)", color: LANDING_COLORS.textMuted }}>
                    <span>{card.footerLeft}</span>
                    <span style={{ color: LANDING_COLORS.emerald }}>{card.footerRight}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
