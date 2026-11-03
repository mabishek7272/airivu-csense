import { useEffect, useRef, useState } from "react";
import { CursorGlow } from "./landing3rdi/CursorGlow";
import { DeploySection } from "./landing3rdi/DeploySection";
import { EdgeHardwareSection } from "./landing3rdi/EdgeHardwareSection";
import { Footer } from "./landing3rdi/Footer";
import { Header } from "./landing3rdi/Header";
import { HeroSection } from "./landing3rdi/HeroSection";
import { MeshSection } from "./landing3rdi/MeshSection";
import { ParticleField, type ParticleFieldHandle } from "./landing3rdi/ParticleField";
import { PipelinesSection } from "./landing3rdi/PipelinesSection";
import { PortalsSection } from "./landing3rdi/PortalsSection";
import { ScrollProgressBar } from "./landing3rdi/ScrollProgressBar";
import { SecuritySection } from "./landing3rdi/SecuritySection";
import { LANDING_COLORS, prefersReducedMotion } from "./landing3rdi/tokens";

const FONT_LINKS = [
  { rel: "preconnect", href: "https://fonts.googleapis.com" },
  { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" as const },
  { rel: "stylesheet", href: "https://fonts.googleapis.com/css2?family=Geist:wght@300..900&family=Geist+Mono:wght@400..700&display=swap" },
  { rel: "stylesheet", href: "https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,300..500,0..1,0&display=swap" },
];

const PAGE_STYLE_ID = "r3-landing-style";
const PAGE_STYLE = `
  html{scroll-behavior:smooth}
  .r3-pulse{animation:r3-pulse 1.3s ease-in-out infinite alternate}
  @keyframes r3-pulse{from{opacity:1}to{opacity:.35}}
  .r3-scan{position:absolute;left:0;right:0;height:2px;top:0;background:linear-gradient(90deg,transparent,rgba(52,211,153,.8),transparent);box-shadow:0 0 18px rgba(52,211,153,.6);pointer-events:none;animation:r3-scan 4.5s linear infinite}
  @keyframes r3-scan{from{top:0%}to{top:100%}}
  .r3-nav-link{color:${LANDING_COLORS.textMuted};font-size:13px}
  .r3-nav-link:hover{color:${LANDING_COLORS.ink}}
  .r3-header-ghost:hover{color:${LANDING_COLORS.ink};border-color:${LANDING_COLORS.borderStrong}}
  .r3-header-cta:hover{background:${LANDING_COLORS.violetLight};color:${LANDING_COLORS.violetInk}}
  .r3-cta-primary:hover{filter:brightness(1.12)}
  .r3-cta-secondary:hover{color:${LANDING_COLORS.ink};border-color:${LANDING_COLORS.borderStrong};background:#18181b}
  .r3-footer-link,.r3-footer-link-violet{color:${LANDING_COLORS.textMuted};text-decoration:none;font-size:13px}
  .r3-footer-link:hover{color:${LANDING_COLORS.ink}}
  .r3-footer-link-violet:hover{color:${LANDING_COLORS.violetLight}}
  @media (prefers-reduced-motion: reduce){
    html{scroll-behavior:auto}
    .r3-pulse,.r3-scan{animation:none}
  }
`;

/** The bare-domain marketing landing page (3rdi.in with no brand slug) - see `isApex`
 *  wiring in App.tsx:50. Full animated enterprise site per the Claude Design handoff
 *  (`3RDI CSense.dc.html`): Three.js particle field morphing between scenes as the
 *  reader scrolls, scroll-jacked hardware carousel, tabbed pipeline showcase, animated
 *  counters, closing marquee. Deliberately its own visual universe (near-black/violet,
 *  Geist fonts) distinct from the rest of this app's "Technical Atmosphere" theme, same
 *  as this page always has been - see landing3rdi/tokens.ts.
 *
 *  Fonts and the handful of global style resets this page needs (`html{scroll-behavior:
 *  smooth}`, decorative keyframes) are injected on mount and removed on unmount so they
 *  never leak onto the rest of the app's routes. */
export function Landing3rdiPage() {
  const rootRef = useRef<HTMLDivElement>(null);
  const particleFieldRef = useRef<ParticleFieldHandle>(null);
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.innerWidth < 960);

  useEffect(() => {
    const links = FONT_LINKS.map((attrs) => {
      const link = document.createElement("link");
      Object.entries(attrs).forEach(([key, value]) => link.setAttribute(key, value as string));
      document.head.appendChild(link);
      return link;
    });
    const style = document.createElement("style");
    style.id = PAGE_STYLE_ID;
    style.textContent = PAGE_STYLE;
    document.head.appendChild(style);
    const previousBodyBackground = document.body.style.background;
    const previousBodyMargin = document.body.style.margin;
    document.body.style.background = LANDING_COLORS.bg;
    document.body.style.margin = "0";

    return () => {
      links.forEach((link) => link.remove());
      style.remove();
      document.body.style.background = previousBodyBackground;
      document.body.style.margin = previousBodyMargin;
    };
  }, []);

  useEffect(() => {
    const onResize = () => setNarrow(window.innerWidth < 960);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    if (prefersReducedMotion()) return;
    let raf = 0;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      const root = rootRef.current;
      if (!root) return;
      const vh = window.innerHeight;
      let target = 0;
      let bestDist = Infinity;
      root.querySelectorAll<HTMLElement>("[data-scene]").forEach((section) => {
        const rect = section.getBoundingClientRect();
        const center = Math.max(rect.top, Math.min(vh / 2, rect.bottom));
        const dist = Math.abs(center - vh / 2);
        if (dist < bestDist) {
          bestDist = dist;
          target = Number(section.dataset.scene);
        }
      });
      particleFieldRef.current?.setScene(target);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div
      ref={rootRef}
      style={{
        position: "relative",
        minHeight: "100vh",
        background: LANDING_COLORS.bg,
        color: LANDING_COLORS.ink,
        fontFamily: "'Geist',system-ui,sans-serif",
        overflowX: "clip",
      }}
    >
      <ParticleField ref={particleFieldRef} />
      <div
        aria-hidden
        style={{ position: "fixed", inset: 0, zIndex: 0, pointerEvents: "none", background: "radial-gradient(ellipse at 50% 45%, transparent 25%, rgba(9,9,11,.88) 85%)" }}
      />
      <CursorGlow />
      <ScrollProgressBar />

      <Header narrow={narrow} />
      <HeroSection />
      <PortalsSection />
      <EdgeHardwareSection />
      <MeshSection />
      <PipelinesSection />
      <SecuritySection />
      <DeploySection />
      <Footer />
    </div>
  );
}
