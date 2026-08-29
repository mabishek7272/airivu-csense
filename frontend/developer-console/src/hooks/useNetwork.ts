"use client";

import { useEffect, useState } from "react";

/** `navigator.onLine` is famously weak - it reports whether an interface is up, not
 *  whether anything is reachable, so a captive portal reads as online. Still worth using:
 *  when it says *offline* it is almost always right, which is the direction that matters
 *  for telling "the API is down" apart from "this machine has no network at all". Ported
 *  from frontend/customer-crm/src/hooks/useNetwork.ts (unchanged - no router dependency).
 */
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
