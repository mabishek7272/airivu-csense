import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { LANDING_EASE, prefersReducedMotion } from "./tokens";

/** Fade-and-rise-in-on-scroll, ported from the mockup's `setupReveal()`
 *  (one shared IntersectionObserver per section there; one small observer per call here -
 *  this page has a few dozen reveal targets at most, not a list that scales). */
export function useReveal(delayMs = 0) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const [visible, setVisible] = useState(prefersReducedMotion());
  const ref = useCallback((node: HTMLElement | null) => setEl(node), []);

  useEffect(() => {
    if (!el || visible) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setVisible(true);
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.12 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [el, visible]);

  const style: CSSProperties = visible
    ? {
        opacity: 1,
        transform: "none",
        transition: `opacity 1s ${LANDING_EASE} ${delayMs}ms, transform 1.2s ${LANDING_EASE} ${delayMs}ms`,
      }
    : {
        opacity: 0,
        transform: "perspective(1200px) translateY(80px) rotateX(-24deg) scale(.95)",
        transformOrigin: "50% 100%",
      };

  return { ref, style };
}
