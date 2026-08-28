import { useEffect, useRef, useState } from "react";

/** Network conditions the UI has to tell apart.
 *
 *  "The server is broken" and "your wifi dropped" produce the same failed fetch, and
 *  showing the first when the second is true sends an operator to check a service that is
 *  fine. They need different words and different actions, so they need different states.
 */

/** `navigator.onLine` is famously weak — it reports whether an interface is up, not
 *  whether anything is reachable, so a captive portal reads as online. It is still worth
 *  using because when it says *offline* it is almost always right, and that is the
 *  direction that matters: a false "you are offline" is rare, and the false "you are
 *  online" is caught by the request failing anyway. */
export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator === "undefined" ? true : navigator.onLine,
  );

  useEffect(() => {
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
  }, []);

  return online;
}

/** Becomes true when something has been loading longer than feels normal.
 *
 *  Deliberately *additive*: it does not replace the content or the spinner, it adds a
 *  note. The request may well still succeed, and swapping to an error screen at four
 *  seconds would throw away a response that arrives at five. All it does is answer the
 *  question the user is already asking — "is this stuck, or just slow?"
 */
export function useSlowRequest(isLoading: boolean, thresholdMs = 4000): boolean {
  const [slow, setSlow] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!isLoading) {
      setSlow(false);
      window.clearTimeout(timer.current);
      return;
    }
    timer.current = window.setTimeout(() => setSlow(true), thresholdMs);
    return () => window.clearTimeout(timer.current);
  }, [isLoading, thresholdMs]);

  return slow;
}

/** The Network Information API where it exists, for an explicit "you are on a slow
 *  connection" rather than an inferred one. Chrome-family only, so it is a bonus signal
 *  and never the sole basis for a message. */
export function useConnectionQuality(): "slow" | "ok" | "unknown" {
  const [quality, setQuality] = useState<"slow" | "ok" | "unknown">("unknown");

  useEffect(() => {
    const connection = (navigator as Navigator & { connection?: EventTarget & {
      effectiveType?: string;
      saveData?: boolean;
    } }).connection;
    if (!connection) return;

    const read = () => {
      const type = connection.effectiveType ?? "";
      // saveData means the user has asked for less data; treating that as "slow" makes
      // the UI lighter for someone who explicitly requested it.
      setQuality(
        type === "slow-2g" || type === "2g" || connection.saveData ? "slow" : "ok",
      );
    };
    read();
    connection.addEventListener("change", read);
    return () => connection.removeEventListener("change", read);
  }, []);

  return quality;
}
