import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { LANDING_COLORS, LANDING_FONT_MONO, prefersReducedMotion } from "./tokens";

type OverlayKey = "boxes" | "thermal" | "vectors" | "heatmap";
const OVERLAY_LABELS: Record<OverlayKey, string> = {
  boxes: "Bounding Boxes",
  thermal: "Thermal Mode",
  vectors: "Trajectory Vectors",
  heatmap: "Heatmap",
};

const DETECTIONS = [
  { depth: 10, top: "44%", left: "44%", width: "16%", height: "35%", color: LANDING_COLORS.violet, bg: "rgba(167,139,250,.1)", label: "Robotic Arm #04 · 99.4%", labelBg: LANDING_COLORS.violet, labelFg: LANDING_COLORS.violetInk },
  { depth: 16, top: "66%", left: "18%", width: "14%", height: "20%", color: LANDING_COLORS.emerald, bg: "rgba(52,211,153,.12)", label: "AMR Pallet Carrier · 98.7%", labelBg: LANDING_COLORS.emerald, labelFg: "#001a12" },
  { depth: 8, top: "46%", left: "70%", width: "15%", height: "26%", color: "#7c3aed", bg: "rgba(124,58,237,.15)", label: "Autonomous Carrier · 99.1%", labelBg: "#7c3aed", labelFg: "#ede9fe" },
];

/** The hero: headline + CTAs (parallax/fade on scroll) above a tilted "live demo" panel
 *  (perspective rotate easing flat as it scrolls into view, plus a subtle mouse-tilt).
 *  Owns its own scroll+pointer rAF loop rather than sharing one globally - this section
 *  is the only place these particular transforms apply, so there's nothing to coordinate
 *  with other sections (contrast ParticleField, which genuinely needs cross-section
 *  awareness of scroll position). */
export function HeroSection() {
  const heroTextRef = useRef<HTMLDivElement>(null);
  const heroTiltRef = useRef<HTMLDivElement>(null);
  const depthRefs = useRef<(HTMLDivElement | null)[]>([]);
  const fpsRef = useRef<HTMLSpanElement>(null);
  const latRef = useRef<HTMLSpanElement>(null);
  const [layers, setLayers] = useState<Record<OverlayKey, boolean>>({
    boxes: true,
    thermal: false,
    vectors: false,
    heatmap: false,
  });

  useEffect(() => {
    if (prefersReducedMotion()) return;
    const mouse = { x: 0, y: 0, tx: 0, ty: 0 };
    const onMove = (e: PointerEvent) => {
      mouse.tx = (e.clientX / window.innerWidth) * 2 - 1;
      mouse.ty = (e.clientY / window.innerHeight) * 2 - 1;
    };
    window.addEventListener("pointermove", onMove, { passive: true });

    let raf = 0;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      mouse.x += (mouse.tx - mouse.x) * 0.06;
      mouse.y += (mouse.ty - mouse.y) * 0.06;
      const vh = window.innerHeight;
      const sy = window.scrollY;

      const heroText = heroTextRef.current;
      if (heroText) {
        heroText.style.transform = `translate3d(0,${sy * 0.35}px,0)`;
        heroText.style.opacity = String(1 - Math.max(0, Math.min(1, sy / (vh * 0.75))));
      }

      const heroTilt = heroTiltRef.current;
      if (heroTilt) {
        const rect = heroTilt.getBoundingClientRect();
        const hp = Math.max(0, Math.min(1, 1 - (rect.top - vh * 0.12) / (vh * 0.75)));
        const eased = hp * hp * (3 - 2 * hp);
        heroTilt.style.transform = `perspective(1400px) rotateX(${(1 - eased) * 34 - mouse.y * 2}deg) rotateY(${mouse.x * 3}deg) scale(${0.82 + 0.18 * eased})`;
      }

      depthRefs.current.forEach((el) => {
        if (!el) return;
        const depth = Number(el.dataset.depth);
        el.style.transform = `translate3d(${mouse.x * depth}px,${mouse.y * depth}px,0)`;
      });
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
    };
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (fpsRef.current) fpsRef.current.textContent = (183.8 + Math.random() * 1.2).toFixed(1);
      if (latRef.current) latRef.current.textContent = (11.1 + Math.random() * 0.7).toFixed(1) + "ms";
    }, 1600);
    return () => window.clearInterval(id);
  }, []);

  const toggle = (key: OverlayKey) => setLayers((s) => ({ ...s, [key]: !s[key] }));
  const heroFilter = layers.thermal
    ? "invert(1) hue-rotate(180deg) contrast(1.25) saturate(1.4)"
    : "brightness(.85) contrast(1.05)";

  return (
    <section
      id="top"
      data-scene="0"
      style={{
        position: "relative",
        zIndex: 1,
        padding: "clamp(72px,13vh,140px) clamp(20px,4vw,48px) 120px",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        textAlign: "center",
      }}
    >
      <div ref={heroTextRef} style={{ display: "flex", flexDirection: "column", alignItems: "center", willChange: "transform,opacity" }}>
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 10,
            padding: "7px 16px",
            borderRadius: 999,
            background: "rgba(24,24,27,.8)",
            border: `1px solid ${LANDING_COLORS.border}`,
            backdropFilter: "blur(8px)",
          }}
        >
          <span className="r3-pulse" style={{ width: 7, height: 7, borderRadius: "50%", background: LANDING_COLORS.violet, boxShadow: `0 0 12px ${LANDING_COLORS.violet}` }} />
          <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 11, letterSpacing: ".18em", color: LANDING_COLORS.violetLight }}>
            3RDI CSENSE 4.0 · AUTONOMOUS VISION MESH
          </span>
        </div>

        <h1 style={{ margin: "28px 0 0", fontSize: "clamp(64px,12vw,176px)", fontWeight: 800, letterSpacing: "-0.055em", lineHeight: 0.88 }}>
          3RDI{" "}
          <span
            style={{
              background: "linear-gradient(100deg,#a78bfa 10%,#c4b5fd 45%,#34d399 95%)",
              WebkitBackgroundClip: "text",
              backgroundClip: "text",
              color: "transparent",
            }}
          >
            CSense
          </span>
        </h1>
        <p style={{ margin: "28px 0 0", fontSize: "clamp(18px,2.2vw,26px)", fontWeight: 600, letterSpacing: "-0.02em", maxWidth: 760, textWrap: "balance" }}>
          Intelligent Vision. Connected Infrastructure. Real-Time Decisions.
        </p>
        <p style={{ margin: "14px 0 0", fontSize: 16, lineHeight: 1.6, color: LANDING_COLORS.textMuted, maxWidth: 600, textWrap: "pretty" }}>
          An integrated AI video intelligence ecosystem connecting intelligent surveillance,
          advanced computer vision, enterprise operations and edge AI infrastructure.
        </p>

        <div style={{ marginTop: 40, display: "flex", flexWrap: "wrap", gap: 12, justifyContent: "center" }}>
          <a
            href="https://edge.3rdi.in/"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-cta-primary"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 10,
              padding: "15px 24px",
              borderRadius: 12,
              fontSize: 14,
              fontWeight: 700,
              color: LANDING_COLORS.violetInk,
              background: "linear-gradient(100deg,#a78bfa,#7c3aed 55%,#c4b5fd)",
              boxShadow: "0 0 32px rgba(167,139,250,.5)",
              border: "1px solid rgba(196,181,253,.4)",
            }}
          >
            <Icon name="memory" size={19} />
            Edge Portal
            <Icon name="north_east" size={16} />
          </a>
          <a
            href="https://demo.3rdi.in/"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-cta-secondary"
            style={{ display: "inline-flex", alignItems: "center", gap: 9, padding: "15px 20px", borderRadius: 12, fontSize: 14, fontWeight: 600, color: LANDING_COLORS.ink, background: "rgba(18,18,21,.85)", border: `1px solid ${LANDING_COLORS.border}` }}
          >
            <Icon name="center_focus_strong" size={19} color={LANDING_COLORS.violet} />
            Live AI Demo
          </a>
          <a
            href="https://console.3rdi.in/login"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-cta-secondary"
            style={{ display: "inline-flex", alignItems: "center", gap: 9, padding: "15px 20px", borderRadius: 12, fontSize: 14, color: LANDING_COLORS.textMuted, background: "rgba(15,15,18,.7)", border: `1px solid ${LANDING_COLORS.border}` }}
          >
            <Icon name="terminal" size={18} />
            Developer Console
          </a>
          <a
            href="https://app.3rdi.in/login"
            target="_blank"
            rel="noopener noreferrer"
            className="r3-cta-secondary"
            style={{ display: "inline-flex", alignItems: "center", gap: 9, padding: "15px 20px", borderRadius: 12, fontSize: 14, color: LANDING_COLORS.textMuted, background: "rgba(15,15,18,.7)", border: `1px solid ${LANDING_COLORS.border}` }}
          >
            <Icon name="corporate_fare" size={18} />
            Customer Login
          </a>
        </div>

        <div style={{ marginTop: 56, display: "flex", flexDirection: "column", alignItems: "center", gap: 10, fontFamily: LANDING_FONT_MONO, fontSize: 10, letterSpacing: ".24em", color: LANDING_COLORS.textFaint }}>
          <span>SCROLL TO ENTER THE MESH</span>
          <span style={{ width: 1, height: 44, background: `linear-gradient(${LANDING_COLORS.violet},transparent)` }} />
        </div>
      </div>

      <div style={{ width: "100%", maxWidth: 1180, marginTop: 40 }}>
        <div
          ref={heroTiltRef}
          style={{
            transformOrigin: "50% 0%",
            transform: "perspective(1400px) rotateX(34deg) scale(.82)",
            willChange: "transform",
            borderRadius: 22,
            background: "rgba(18,18,21,.88)",
            border: `1px solid ${LANDING_COLORS.border}`,
            padding: 12,
            textAlign: "left",
            boxShadow: "0 80px 160px -50px rgba(124,58,237,.55), 0 0 0 1px rgba(167,139,250,.08)",
          }}
        >
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              padding: "10px 14px",
              background: LANDING_COLORS.bg,
              border: "1px solid rgba(39,39,42,.7)",
              borderRadius: 12,
              marginBottom: 12,
              fontFamily: LANDING_FONT_MONO,
              fontSize: 12,
            }}
          >
            <span style={{ display: "flex", alignItems: "center", gap: 8, color: LANDING_COLORS.emerald, fontWeight: 600 }}>
              <span className="r3-pulse" style={{ width: 7, height: 7, borderRadius: "50%", background: LANDING_COLORS.emerald }} />
              STREAM_01: INDUSTRIAL_GATE_NORTH
            </span>
            <span style={{ display: "flex", gap: 18, color: LANDING_COLORS.textMuted, flexWrap: "wrap" }}>
              <span>
                FPS <strong ref={fpsRef} style={{ color: LANDING_COLORS.ink }}>184.2</strong>
              </span>
              <span>
                Latency <strong ref={latRef} style={{ color: LANDING_COLORS.emerald }}>11.4ms</strong>
              </span>
              <span>
                Streams <strong style={{ color: LANDING_COLORS.ink }}>1,420+</strong>
              </span>
              <span>
                Model <span style={{ color: LANDING_COLORS.violetLight }}>YOLOv10-TensorRT</span>
              </span>
            </span>
          </div>

          <div style={{ position: "relative", width: "100%", height: "clamp(300px,50vw,600px)", borderRadius: 12, overflow: "hidden", background: LANDING_COLORS.bg, border: `1px solid ${LANDING_COLORS.border}` }}>
            <div
              aria-hidden
              style={{
                position: "absolute",
                inset: -20,
                background: "radial-gradient(circle at 30% 30%, #2a1f3d, #0f0c17 70%)",
                filter: heroFilter,
                transition: "filter .5s",
              }}
            />
            <div className="r3-scan" />
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none", opacity: layers.boxes ? 1 : 0, transition: "opacity .3s" }}>
              {DETECTIONS.map((d) => (
                <div
                  key={d.label}
                  ref={(el) => (depthRefs.current[d.depth] = el)}
                  data-depth={d.depth}
                  style={{ position: "absolute", top: d.top, left: d.left, width: d.width, height: d.height, border: `2px solid ${d.color}`, background: d.bg, borderRadius: 4 }}
                >
                  <div style={{ position: "absolute", top: -24, left: 0, background: d.labelBg, color: d.labelFg, fontFamily: LANDING_FONT_MONO, fontSize: 10, padding: "3px 7px", borderRadius: 4, fontWeight: 700, whiteSpace: "nowrap" }}>
                    {d.label}
                  </div>
                </div>
              ))}
            </div>
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none", opacity: layers.vectors ? 1 : 0, transition: "opacity .3s" }}>
              <svg viewBox="0 0 1000 600" preserveAspectRatio="none" style={{ width: "100%", height: "100%" }} fill="none">
                <path d="M 220 480 Q 260 420 310 400" stroke={LANDING_COLORS.violet} strokeDasharray="4 4" strokeWidth={2} />
                <circle cx={310} cy={400} r={4} fill={LANDING_COLORS.violet} />
                <path d="M 680 500 Q 640 430 580 390" stroke={LANDING_COLORS.emerald} strokeDasharray="6 3" strokeWidth={2} />
                <circle cx={580} cy={390} r={4} fill={LANDING_COLORS.emerald} />
              </svg>
            </div>
            <div
              style={{
                position: "absolute",
                inset: 0,
                pointerEvents: "none",
                opacity: layers.heatmap ? 1 : 0,
                transition: "opacity .3s",
                mixBlendMode: "screen",
                background: "radial-gradient(ellipse at 52% 62%, rgba(124,58,237,.55), rgba(52,211,153,.25) 35%, transparent 65%)",
              }}
            />
            <div style={{ position: "absolute", top: 14, left: 14, display: "flex", alignItems: "center", gap: 8, fontFamily: LANDING_FONT_MONO, fontSize: 11, color: LANDING_COLORS.emerald, background: "rgba(18,18,21,.8)", backdropFilter: "blur(6px)", padding: "5px 10px", borderRadius: 6, border: "1px solid rgba(39,39,42,.7)" }}>
              <span className="r3-pulse" style={{ width: 7, height: 7, borderRadius: "50%", background: LANDING_COLORS.danger }} />
              REC [EDGE-JETSON-09]
            </div>
            <div style={{ position: "absolute", bottom: 14, left: 14, fontFamily: LANDING_FONT_MONO, fontSize: 10, color: LANDING_COLORS.textMuted, background: "rgba(18,18,21,.8)", backdropFilter: "blur(6px)", padding: "5px 10px", borderRadius: 6, border: "1px solid rgba(39,39,42,.7)" }}>
              RES 3840×2160 @ 60Hz · H.265 (VA-API)
            </div>
            <div style={{ position: "absolute", bottom: 14, right: 14, display: "flex", alignItems: "center", gap: 8, fontFamily: LANDING_FONT_MONO, fontSize: 12, background: "rgba(12,12,15,.9)", backdropFilter: "blur(6px)", padding: "7px 12px", borderRadius: 8, border: `1px solid ${LANDING_COLORS.border}` }}>
              <Icon name="verified" size={16} color={LANDING_COLORS.emerald} />
              Perimeter Zone: <strong style={{ color: LANDING_COLORS.emerald }}>SAFE</strong>
            </div>
          </div>

          <div style={{ marginTop: 12, display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "4px 4px 2px" }}>
            <span style={{ display: "flex", alignItems: "center", gap: 6, fontFamily: LANDING_FONT_MONO, fontSize: 11, color: LANDING_COLORS.textMuted, letterSpacing: ".08em" }}>
              <Icon name="tune" size={16} />
              ACTIVE VISION OVERLAYS
            </span>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {(Object.keys(OVERLAY_LABELS) as OverlayKey[]).map((key) => {
                const on = layers[key];
                return (
                  <button
                    key={key}
                    onClick={() => toggle(key)}
                    style={{
                      padding: "8px 13px",
                      borderRadius: 8,
                      fontFamily: LANDING_FONT_MONO,
                      fontSize: 12,
                      cursor: "pointer",
                      background: on ? LANDING_COLORS.violet : "#18181b",
                      color: on ? LANDING_COLORS.violetInk : LANDING_COLORS.textMuted,
                      border: `1px solid ${on ? LANDING_COLORS.violetLight : LANDING_COLORS.border}`,
                      transition: "all .2s",
                    }}
                  >
                    {OVERLAY_LABELS[key]}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
