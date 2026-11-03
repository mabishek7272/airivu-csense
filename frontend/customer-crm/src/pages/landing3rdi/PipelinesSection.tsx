import { useState } from "react";
import { Icon } from "./Icon";
import { useReveal } from "./useReveal";
import { useTiltGlare } from "./useTiltGlare";
import { LANDING_COLORS, LANDING_FONT_MONO } from "./tokens";

interface Pipeline {
  key: string;
  name: string;
  sub: string;
  icon: string;
  recall: string;
  title: string;
  detect: string;
  tag: string;
  conf: string;
  lat: string;
  map: string;
  exp: string;
  gradient: string;
  box: { top: string; left: string; width: string; height: string };
}

const PIPELINES: Pipeline[] = [
  {
    key: "ppe",
    name: "PPE & Safety Monitoring",
    sub: "Hardhats, vests, eye protection & zone violations",
    icon: "health_and_safety",
    recall: "98.9% recall",
    title: "Ops Center · PPE & Safety",
    detect: "Hardhat · Vest · Line Breach",
    tag: "HARDHAT 0.99",
    conf: "98.9%",
    lat: "< 7.4ms",
    map: "96.8%",
    exp: "TensorRT 10",
    gradient: "radial-gradient(circle at 44% 50%, #203826, #0b0c0d 70%)",
    box: { top: "30%", left: "18%", width: "18%", height: "36%" },
  },
  {
    key: "anpr",
    name: "ANPR & License Plate OCR",
    sub: "Multi-lane recognition, vehicle classification",
    icon: "pin",
    recall: "99.4% recall",
    title: "High-Speed ANPR & OCR",
    detect: "MH-12-AB-9021 · Sedan",
    tag: "PLATE 0.99",
    conf: "99.4%",
    lat: "< 6.8ms",
    map: "98.2%",
    exp: "ONNX / TRT",
    gradient: "radial-gradient(circle at 50% 55%, #2c2a1f, #0b0c0d 70%)",
    box: { top: "52%", left: "54%", width: "20%", height: "16%" },
  },
  {
    key: "perimeter",
    name: "Intrusion & Perimeter Detection",
    sub: "Virtual tripwires, directional loitering alerts",
    icon: "fence",
    recall: "99.7% recall",
    title: "Virtual Tripwire & Intrusion",
    detect: "Zone 04 Breach · Vector 242°",
    tag: "INTRUDER 0.99",
    conf: "99.7%",
    lat: "< 5.2ms",
    map: "97.5%",
    exp: "DeepStream",
    gradient: "radial-gradient(circle at 60% 35%, #2a1f2e, #0b0c0d 70%)",
    box: { top: "24%", left: "66%", width: "14%", height: "40%" },
  },
  {
    key: "crowd",
    name: "Crowd Density & Flow Dynamics",
    sub: "Congestion heatmaps, bidirectional pass counts",
    icon: "groups",
    recall: "98.2% recall",
    title: "Crowd Density & Dwell",
    detect: "3.4 p/m² · Dwell 4m 12s",
    tag: "CLUSTER 0.98",
    conf: "98.2%",
    lat: "< 11.2ms",
    map: "95.1%",
    exp: "CUDA TRT",
    gradient: "radial-gradient(circle at 40% 40%, #1f2a38, #0b0c0d 70%)",
    box: { top: "18%", left: "28%", width: "44%", height: "46%" },
  },
  {
    key: "custom",
    name: "Custom TensorRT Pipelines",
    sub: "Deploy custom ONNX, PyTorch, or 3RDI AutoML",
    icon: "extension",
    recall: "Zero-Code",
    title: "Custom Pipeline Ingestion",
    detect: "custom_v8_fp16.engine · Ready",
    tag: "CUSTOM CLASS",
    conf: "Configurable",
    lat: "< 8.0ms",
    map: "Custom",
    exp: "PyTorch / ONNX",
    gradient: "radial-gradient(circle at 48% 45%, #262626, #0b0c0d 70%)",
    box: { top: "36%", left: "40%", width: "22%", height: "28%" },
  },
];

/** "Precision vision pipelines": a tabbed list on the left drives a live preview panel
 *  on the right, ported from the mockup's `PIPES` data + `pipeTabs`/`cur` derivation. */
export function PipelinesSection() {
  const heading = useReveal(0);
  const list = useReveal(0);
  const preview = useReveal(150);
  const { cardRef, glareRef, onPointerMove, onPointerLeave } = useTiltGlare();
  const [active, setActive] = useState("ppe");
  const current = PIPELINES.find((p) => p.key === active)!;

  return (
    <section id="pipelines" data-scene="4" style={{ position: "relative", zIndex: 1, padding: "clamp(100px,14vh,160px) clamp(20px,4vw,48px)" }}>
      <div style={{ maxWidth: 1240, margin: "0 auto" }}>
        <div ref={heading.ref} style={{ ...heading.style, display: "flex", flexWrap: "wrap", alignItems: "flex-end", justifyContent: "space-between", gap: 24, marginBottom: 56 }}>
          <div style={{ maxWidth: 680 }}>
            <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 12, letterSpacing: ".2em", color: LANDING_COLORS.violet }}>INFERENCE MODEL MATRIX</span>
            <h2 style={{ margin: "14px 0 0", fontSize: "clamp(36px,5vw,64px)", fontWeight: 800, letterSpacing: "-0.04em", lineHeight: 1 }}>Precision vision pipelines</h2>
            <p style={{ margin: "16px 0 0", fontSize: 16, lineHeight: 1.6, color: LANDING_COLORS.textMuted }}>
              Production-hardened computer vision, pre-tuned for enterprise safety, perimeter security, and operational intelligence.
            </p>
          </div>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "8px 12px", borderRadius: 9, background: LANDING_COLORS.surfaceStrong, border: `1px solid ${LANDING_COLORS.border}`, fontFamily: LANDING_FONT_MONO, fontSize: 12, color: LANDING_COLORS.violet }}>
            <Icon name="auto_awesome" size={16} />
            3RDI AutoML Engine Active
          </span>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,420px),1fr))", gap: 32, alignItems: "start" }}>
          <div ref={list.ref} style={{ ...list.style, display: "flex", flexDirection: "column", gap: 10 }}>
            {PIPELINES.map((p) => {
              const on = p.key === active;
              return (
                <button
                  key={p.key}
                  onClick={() => setActive(p.key)}
                  style={{
                    textAlign: "left",
                    padding: "18px 20px",
                    borderRadius: 14,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 16,
                    cursor: "pointer",
                    color: LANDING_COLORS.ink,
                    fontFamily: "inherit",
                    background: on ? "rgba(30,30,34,.94)" : "rgba(15,15,18,.72)",
                    backdropFilter: "blur(10px)",
                    border: `1px solid ${on ? "rgba(167,139,250,.55)" : LANDING_COLORS.border}`,
                    transform: `translateX(${on ? "10px" : "0px"})`,
                    boxShadow: on ? "0 20px 60px -20px rgba(124,58,237,.6)" : "none",
                    transition: "all .4s cubic-bezier(.2,.7,.2,1)",
                  }}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: 14 }}>
                    <Icon name={p.icon} size={24} color={on ? LANDING_COLORS.violet : LANDING_COLORS.textFaint} />
                    <span style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                      <span style={{ fontSize: 15, fontWeight: 700 }}>{p.name}</span>
                      <span style={{ fontSize: 12, color: LANDING_COLORS.textMuted }}>{p.sub}</span>
                    </span>
                  </span>
                  <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 12, color: on ? LANDING_COLORS.emerald : LANDING_COLORS.textFaint, whiteSpace: "nowrap" }}>{p.recall}</span>
                </button>
              );
            })}
          </div>

          <div ref={preview.ref} style={preview.style}>
            <div
              ref={cardRef}
              onPointerMove={onPointerMove}
              onPointerLeave={onPointerLeave}
              style={{
                position: "relative",
                padding: 18,
                borderRadius: 22,
                background: LANDING_COLORS.surfaceStrong,
                backdropFilter: "blur(12px)",
                border: `1px solid ${LANDING_COLORS.border}`,
                display: "flex",
                flexDirection: "column",
                gap: 14,
                boxShadow: "0 40px 100px -40px rgba(124,58,237,.5)",
                transformStyle: "preserve-3d",
                transition: "transform .4s cubic-bezier(.2,.7,.2,1), border-color .3s",
              }}
            >
              <div ref={glareRef} style={{ position: "absolute", inset: 0, borderRadius: 22, pointerEvents: "none", opacity: 0, transition: "opacity .3s" }} />
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 10, fontFamily: LANDING_FONT_MONO, fontSize: 12 }}>
                <span style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 2, background: LANDING_COLORS.violet }} />
                  {current.title}
                </span>
                <span style={{ display: "flex", alignItems: "center", gap: 6, color: LANDING_COLORS.emerald }}>
                  <span className="r3-pulse" style={{ width: 7, height: 7, borderRadius: "50%", background: LANDING_COLORS.emerald }} />
                  Live Analytics Stream
                </span>
              </div>
              <div style={{ position: "relative", height: "clamp(260px,30vw,380px)", borderRadius: 14, overflow: "hidden", background: LANDING_COLORS.bg, border: `1px solid ${LANDING_COLORS.border}`, transform: "translateZ(30px)" }}>
                <div aria-hidden style={{ position: "absolute", inset: 0, background: current.gradient, transition: "background .6s" }} />
                <div
                  style={{
                    position: "absolute",
                    top: current.box.top,
                    left: current.box.left,
                    width: current.box.width,
                    height: current.box.height,
                    border: `2px solid ${LANDING_COLORS.emerald}`,
                    borderRadius: 4,
                    background: "rgba(52,211,153,.1)",
                    boxShadow: "0 0 24px rgba(52,211,153,.35)",
                    transition: "all .7s cubic-bezier(.2,.7,.2,1)",
                  }}
                >
                  <span style={{ position: "absolute", top: -22, left: -2, background: LANDING_COLORS.emerald, color: "#001a12", fontFamily: LANDING_FONT_MONO, fontSize: 10, fontWeight: 700, padding: "3px 7px", borderRadius: 4, whiteSpace: "nowrap" }}>
                    {current.tag}
                  </span>
                </div>
                <div style={{ position: "absolute", top: 12, right: 12, background: "rgba(124,58,237,.85)", color: "#ede9fe", fontFamily: LANDING_FONT_MONO, fontSize: 10, fontWeight: 600, padding: "5px 10px", borderRadius: 6 }}>
                  GLOBAL OPS HUD
                </div>
                <div style={{ position: "absolute", left: 12, right: 12, bottom: 12, display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 10, padding: 12, borderRadius: 10, background: "rgba(12,12,15,.9)", backdropFilter: "blur(8px)", border: `1px solid ${LANDING_COLORS.border}`, fontFamily: LANDING_FONT_MONO, fontSize: 12 }}>
                  <span style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
                    <Icon name="check_circle" size={16} color={LANDING_COLORS.emerald} />
                    {current.detect}
                  </span>
                  <span style={{ color: LANDING_COLORS.textMuted }}>
                    Confidence <strong style={{ color: LANDING_COLORS.emerald }}>{current.conf}</strong>
                  </span>
                </div>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3,minmax(0,1fr))", gap: 10, transform: "translateZ(20px)" }}>
                <div style={{ padding: "14px 10px", borderRadius: 12, background: "#0f0f12", border: `1px solid ${LANDING_COLORS.border}`, textAlign: "center" }}>
                  <div style={{ fontSize: 11, color: LANDING_COLORS.textMuted }}>Latency</div>
                  <div style={{ marginTop: 6, fontFamily: LANDING_FONT_MONO, fontWeight: 700, fontSize: 15 }}>{current.lat}</div>
                </div>
                <div style={{ padding: "14px 10px", borderRadius: 12, background: "#0f0f12", border: `1px solid ${LANDING_COLORS.border}`, textAlign: "center" }}>
                  <div style={{ fontSize: 11, color: LANDING_COLORS.textMuted }}>mAP@50</div>
                  <div style={{ marginTop: 6, fontFamily: LANDING_FONT_MONO, fontWeight: 700, fontSize: 15, color: LANDING_COLORS.emerald }}>{current.map}</div>
                </div>
                <div style={{ padding: "14px 10px", borderRadius: 12, background: "#0f0f12", border: `1px solid ${LANDING_COLORS.border}`, textAlign: "center" }}>
                  <div style={{ fontSize: 11, color: LANDING_COLORS.textMuted }}>Export</div>
                  <div style={{ marginTop: 6, fontFamily: LANDING_FONT_MONO, fontWeight: 700, fontSize: 15, color: LANDING_COLORS.violetLight }}>{current.exp}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
