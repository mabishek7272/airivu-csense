import { useEffect, useRef } from "react";

/** The bare-domain splash (3rdi.in with no brand slug in the URL). 3RDI is the parent
 *  company operating every white-label brand on this platform (eAIgleye, Apti SafeGuard,
 *  Airivu CSense); this page is not itself a white-labeled product surface, so it does
 *  not go through BrandProvider and carries no login form or per-brand navigation - a
 *  visitor here is expected to already know, or be given, their own brand's specific URL
 *  (3rdi.in/eaigleye, 3rdi.in/apti, ...).
 *
 *  The one link this page does carry - Edge - is deliberately not a brand's product
 *  surface either: it goes to edge.3rdi.in, a separate static portfolio site for edge
 *  hardware (Raspberry Pi, Jetson, etc.), which is why it's a plain external link rather
 *  than a route inside this app.
 *
 *  The 3D effect is CSS transforms driven directly via refs, not a WebGL/Three.js scene -
 *  this is one splash page, not somewhere worth paying a ~600KB dependency for the rest
 *  of this app to carry. `perspective` on the stage plus per-layer `translateZ` and a
 *  mouse-driven `rotateX/rotateY` on the card is the same trick real product pages use
 *  (Stripe, Linear) for a convincing sense of depth at a fraction of the cost. Style
 *  mutations go straight through refs rather than React state so a mousemove at 60fps
 *  never triggers a re-render - state churn on every pixel of pointer movement is exactly
 *  the kind of thing that makes a "3D" page feel janky instead of premium.
 */
export function Landing3rdiPage() {
  const stageRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const shineRef = useRef<HTMLDivElement>(null);
  const parallaxRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    const stage = stageRef.current;
    const card = cardRef.current;
    const shine = shineRef.current;
    if (!stage || !card || !shine) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduceMotion) return;

    let raf = 0;
    let targetX = 0;
    let targetY = 0;
    let currentX = 0;
    let currentY = 0;

    function onPointerMove(e: PointerEvent) {
      const rect = stage!.getBoundingClientRect();
      // -1..1 across each axis, 0 at center.
      targetX = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      targetY = ((e.clientY - rect.top) / rect.height) * 2 - 1;
    }

    function onPointerLeave() {
      targetX = 0;
      targetY = 0;
    }

    function tick() {
      // Simple spring-ish easing toward the target - cheap, and it's what keeps the
      // tilt feeling like it has weight instead of snapping straight to the cursor.
      currentX += (targetX - currentX) * 0.08;
      currentY += (targetY - currentY) * 0.08;

      const maxTilt = 14;
      card!.style.transform = `rotateX(${(-currentY * maxTilt).toFixed(2)}deg) rotateY(${(currentX * maxTilt).toFixed(2)}deg)`;
      shine!.style.background = `radial-gradient(circle at ${50 + currentX * 40}% ${50 + currentY * 40}%, rgba(255,255,255,0.65), rgba(255,255,255,0) 55%)`;
      // The shadow moves opposite the tilt (a card leaning left casts its shadow to the
      // right) and grows on the axis the card lifts toward - without this, a rotateX/Y
      // transform alone reads as flat in a still frame; a shifting, asymmetric shadow is
      // what actually sells "this is a physical object catching light", not just motion.
      const shadowX = (-currentX * 28).toFixed(1);
      const shadowY = (18 - currentY * 20).toFixed(1);
      card!.style.boxShadow =
        `${shadowX}px ${shadowY}px 60px -18px rgba(152,19,78,0.35), ` +
        `0 10px 24px -8px rgba(0,0,0,0.10)`;

      parallaxRefs.current.forEach((el, i) => {
        if (!el) return;
        const depth = (i + 1) * 10;
        el.style.transform = `translate3d(${currentX * depth}px, ${currentY * depth}px, 0)`;
      });

      raf = requestAnimationFrame(tick);
    }

    stage.addEventListener("pointermove", onPointerMove);
    stage.addEventListener("pointerleave", onPointerLeave);
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      stage.removeEventListener("pointermove", onPointerMove);
      stage.removeEventListener("pointerleave", onPointerLeave);
    };
  }, []);

  return (
    <main
      ref={stageRef}
      style={{
        position: "relative",
        minHeight: "100vh",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 40,
        // Creamy white base, not the app's own dark --bg token - deliberately not a
        // white-labeled surface (see this file's own docstring).
        background: "#FAF6EF",
        perspective: "1200px",
      }}
    >
      <style>{`
        @keyframes drift3rdi {
          0%   { transform: translate3d(0, 0, 0) scale(1); }
          50%  { transform: translate3d(0, -18px, 0) scale(1.04); }
          100% { transform: translate3d(0, 0, 0) scale(1); }
        }
        @keyframes fadeUp3rdi {
          from { opacity: 0; transform: translateY(14px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        .r3-blob { animation: drift3rdi 9s ease-in-out infinite; }
        .r3-enter { animation: fadeUp3rdi 0.7s ease-out both; }
        @media (prefers-reduced-motion: reduce) {
          .r3-blob { animation: none; }
          .r3-enter { animation: none; }
        }
      `}</style>

      {/* Ambient depth layer - soft blurred color fields in the brand's own two accent
          colors (matching the eye logo: maroon iris, warm gold wordmark), drifting
          slowly and parallax-shifted opposite the cursor via the refs above. Pure
          atmosphere, never enough contrast to compete with the logo. */}
      <div
        ref={(el) => (parallaxRefs.current[0] = el)}
        className="r3-blob"
        style={{
          position: "absolute", top: "8%", left: "12%", width: 420, height: 420,
          borderRadius: "50%", background: "radial-gradient(circle, rgba(152,19,78,0.16), rgba(152,19,78,0) 70%)",
          filter: "blur(10px)", pointerEvents: "none",
        }}
      />
      <div
        ref={(el) => (parallaxRefs.current[1] = el)}
        className="r3-blob"
        style={{
          position: "absolute", bottom: "10%", right: "14%", width: 380, height: 380,
          borderRadius: "50%", background: "radial-gradient(circle, rgba(196,142,74,0.18), rgba(196,142,74,0) 70%)",
          filter: "blur(10px)", pointerEvents: "none", animationDelay: "-4s",
        }}
      />
      <div
        ref={(el) => (parallaxRefs.current[2] = el)}
        style={{
          position: "absolute", inset: 0, pointerEvents: "none", opacity: 0.35,
          backgroundImage: "radial-gradient(rgba(152,19,78,0.14) 1px, transparent 1px)",
          backgroundSize: "28px 28px",
          maskImage: "radial-gradient(circle at 50% 45%, black, transparent 68%)",
          WebkitMaskImage: "radial-gradient(circle at 50% 45%, black, transparent 68%)",
        }}
      />

      {/* The card itself - perspective tilt lives here, the shine overlay rides on top
          of the logo so the "glossy surface" reads as one object moving together. */}
      <div
        ref={cardRef}
        className="r3-enter"
        style={{
          position: "relative",
          transformStyle: "preserve-3d",
          transition: "transform 0.15s ease-out",
          borderRadius: 24,
          padding: "48px 56px",
          background: "rgba(255,255,255,0.55)",
          boxShadow: "0 30px 60px -20px rgba(152,19,78,0.25), 0 10px 24px -8px rgba(0,0,0,0.08)",
          backdropFilter: "blur(6px)",
        }}
      >
        <div ref={shineRef} style={{ position: "absolute", inset: 0, borderRadius: 24, pointerEvents: "none" }} />
        <img
          src="/3rdi-logo.png"
          alt="3RDI"
          style={{
            position: "relative",
            maxWidth: "min(420px, 70vw)",
            width: "100%",
            height: "auto",
            display: "block",
          }}
        />
      </div>

      <a
        href="https://edge.3rdi.in"
        className="r3-enter"
        style={{
          position: "relative",
          padding: "10px 28px",
          borderRadius: 999,
          border: "1px solid #98134E",
          color: "#98134E",
          fontWeight: 600,
          fontSize: 14,
          letterSpacing: "0.04em",
          textDecoration: "none",
          textTransform: "uppercase",
          background: "rgba(255,255,255,0.6)",
          transition: "transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease",
          animationDelay: "0.15s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.transform = "translateY(-3px)";
          e.currentTarget.style.boxShadow = "0 12px 24px -10px rgba(152,19,78,0.35)";
          e.currentTarget.style.background = "#98134E";
          e.currentTarget.style.color = "#FAF6EF";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.transform = "translateY(0)";
          e.currentTarget.style.boxShadow = "none";
          e.currentTarget.style.background = "rgba(255,255,255,0.6)";
          e.currentTarget.style.color = "#98134E";
        }}
      >
        Edge
      </a>
    </main>
  );
}
