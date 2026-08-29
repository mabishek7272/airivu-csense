import { useEffect, useState } from "react";

// CSS alone (@media prefers-reduced-motion) can silence a transition, but it can't stop
// a JS setInterval — the auto-advance timer itself needs this to know whether to run at
// all, not just how to animate when it does.
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return reduced;
}
