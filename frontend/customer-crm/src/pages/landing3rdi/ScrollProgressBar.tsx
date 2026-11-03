import { useEffect, useRef } from "react";
import { prefersReducedMotion } from "./tokens";

/** Thin bar across the very top tracking scroll position through the whole page. */
export function ScrollProgressBar() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || prefersReducedMotion()) return;
    let raf = 0;
    const onScroll = () => {
      raf = requestAnimationFrame(() => {
        const doc = document.documentElement;
        const max = Math.max(1, doc.scrollHeight - window.innerHeight);
        el.style.transform = `scaleX(${window.scrollY / max})`;
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
    <div
      ref={ref}
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        height: 2,
        background: "linear-gradient(90deg,#7c3aed,#a78bfa,#34d399)",
        transformOrigin: "0 50%",
        transform: "scaleX(0)",
        zIndex: 60,
      }}
    />
  );
}
