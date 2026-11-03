import { useEffect, useRef, useState } from "react";
import { prefersReducedMotion } from "./tokens";

/** Counts up from 0 once scrolled into view, ported from the mockup's `setupCounts()`. */
export function CountUp({
  to,
  decimals = 0,
  prefix = "",
  suffix = "",
  style,
}: {
  to: number;
  decimals?: number;
  prefix?: string;
  suffix?: string;
  style?: React.CSSProperties;
}) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const [value, setValue] = useState(prefersReducedMotion() ? to : 0);
  const raf = useRef(0);

  useEffect(() => {
    if (!el || prefersReducedMotion()) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const t0 = performance.now();
          const step = (now: number) => {
            const k = Math.min(1, (now - t0) / 1800);
            const eased = 1 - Math.pow(1 - k, 3);
            setValue(to * eased);
            if (k < 1) raf.current = requestAnimationFrame(step);
          };
          raf.current = requestAnimationFrame(step);
          observer.unobserve(entry.target);
        }
      },
      { threshold: 0.5 },
    );
    observer.observe(el);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(raf.current);
    };
  }, [el, to]);

  return (
    <div ref={setEl} style={style}>
      {prefix}
      {value.toFixed(decimals)}
      {suffix}
    </div>
  );
}
