import { useEffect, useRef } from "react";
import { prefersReducedMotion } from "./tokens";

/** Soft violet glow that follows the pointer, purely decorative background chrome. */
export function CursorGlow() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || prefersReducedMotion()) return;
    const onMove = (e: PointerEvent) => {
      el.style.transform = `translate(${e.clientX - 300}px,${e.clientY - 300}px)`;
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => window.removeEventListener("pointermove", onMove);
  }, []);

  return (
    <div
      ref={ref}
      style={{
        position: "fixed",
        left: 0,
        top: 0,
        width: 600,
        height: 600,
        borderRadius: "50%",
        background: "radial-gradient(circle, rgba(124,58,237,.16), transparent 60%)",
        pointerEvents: "none",
        zIndex: 0,
        willChange: "transform",
        transform: "translate(-999px,-999px)",
      }}
    />
  );
}
