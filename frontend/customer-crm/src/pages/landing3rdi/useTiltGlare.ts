import { useCallback, useRef, type PointerEvent as ReactPointerEvent } from "react";
import { prefersReducedMotion } from "./tokens";

const TILT_STRENGTH = 12;

/** Pointer-driven card tilt + radial glare, ported from the mockup's `setupTilt()`.
 *  Mutates style directly via refs rather than React state - same rationale as this
 *  app's existing Landing3rdiPage 3D tilt effect: a mousemove at 60fps shouldn't trigger
 *  a re-render. */
export function useTiltGlare() {
  const cardRef = useRef<HTMLDivElement>(null);
  const glareRef = useRef<HTMLDivElement>(null);

  const onPointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (prefersReducedMotion()) return;
    const card = cardRef.current;
    if (!card) return;
    const rect = card.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    card.style.transform = `perspective(1000px) rotateX(${(0.5 - y) * TILT_STRENGTH}deg) rotateY(${(x - 0.5) * TILT_STRENGTH}deg) translateY(-6px)`;
    const glare = glareRef.current;
    if (glare) {
      glare.style.opacity = "1";
      glare.style.background = `radial-gradient(circle at ${x * 100}% ${y * 100}%, rgba(167,139,250,.22), transparent 55%)`;
    }
  }, []);

  const onPointerLeave = useCallback(() => {
    const card = cardRef.current;
    if (card) card.style.transform = "perspective(1000px) rotateX(0deg) rotateY(0deg)";
    const glare = glareRef.current;
    if (glare) glare.style.opacity = "0";
  }, []);

  return { cardRef, glareRef, onPointerMove, onPointerLeave };
}
