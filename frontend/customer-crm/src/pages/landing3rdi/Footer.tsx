import { Icon } from "./Icon";
import { LANDING_COLORS, LANDING_FONT_MONO } from "./tokens";

export function Footer() {
  return (
    <footer style={{ position: "relative", zIndex: 1, background: LANDING_COLORS.bg, borderTop: `1px solid ${LANDING_COLORS.border}`, padding: "56px clamp(20px,4vw,48px) 36px", color: LANDING_COLORS.textMuted }}>
      <div style={{ maxWidth: 1240, margin: "0 auto", display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))", gap: 40, paddingBottom: 40, borderBottom: `1px solid ${LANDING_COLORS.border}` }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <span style={{ display: "flex", alignItems: "center", gap: 10, color: LANDING_COLORS.ink, fontWeight: 700, letterSpacing: "-0.02em" }}>
            <span style={{ width: 30, height: 30, borderRadius: 8, border: `1px solid ${LANDING_COLORS.border}`, background: "#121215", display: "grid", placeItems: "center", color: LANDING_COLORS.violet }}>
              <Icon name="visibility" size={18} />
            </span>
            3RDI CSense
          </span>
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, maxWidth: 300 }}>
            Ultra-low latency vision infrastructure for enterprise spatial intelligence and distributed edge orchestration.
          </p>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontFamily: LANDING_FONT_MONO, fontSize: 11, color: LANDING_COLORS.emerald, letterSpacing: ".08em" }}>
            <span className="r3-pulse" style={{ width: 7, height: 7, borderRadius: "50%", background: LANDING_COLORS.emerald }} />
            ALL EDGE CLUSTERS OPERATIONAL
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13 }}>
          <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 11, letterSpacing: ".14em", color: LANDING_COLORS.ink, marginBottom: 4 }}>ECOSYSTEM PORTALS</span>
          <a href="https://app.3rdi.in/login" className="r3-footer-link">Customer CRM</a>
          <a href="https://console.3rdi.in/login" className="r3-footer-link">Developer Console</a>
          <a href="https://demo.3rdi.in/" className="r3-footer-link">Live AI Demo</a>
          <a href="https://edge.3rdi.in/" style={{ color: LANDING_COLORS.violet }} className="r3-footer-link-violet">Edge Portal</a>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13 }}>
          <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 11, letterSpacing: ".14em", color: LANDING_COLORS.ink, marginBottom: 4 }}>HARDWARE</span>
          <span>NVIDIA Jetson Orin</span>
          <span>Raspberry Pi 5 / CM4</span>
          <span>Spaider Edge Appliances</span>
          <span>On-prem GPU Clusters</span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13 }}>
          <span style={{ fontFamily: LANDING_FONT_MONO, fontSize: 11, letterSpacing: ".14em", color: LANDING_COLORS.ink, marginBottom: 4 }}>ASSURANCE</span>
          <span>SOC 2 Type II</span>
          <span>ISO/IEC 27001</span>
          <span>GDPR / DPDP</span>
        </div>
      </div>
      <div style={{ maxWidth: 1240, margin: "24px auto 0", display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 12, fontSize: 12 }}>
        <span>© {new Date().getFullYear()} 3RDI CSense. All rights reserved.</span>
        <span style={{ fontFamily: LANDING_FONT_MONO, color: LANDING_COLORS.textFaint }}>EDGE · MESH · DECISION</span>
      </div>
    </footer>
  );
}
