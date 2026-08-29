import { useEffect, useRef, useState } from "react";
import { API_BASE, apiFetch } from "../api/client";

export type SocketStatus = "connecting" | "open" | "closed";

interface IncidentEvent {
  type: string;
  incident_id: string;
  occurred_at: string;
}

interface TicketResponse {
  ticket: string;
  expires_in: number;
}

const MAX_BACKOFF_MS = 10000;

function wsUrl(ticket: string): string {
  // API_BASE is empty in every real deployment (same-origin behind Traefik, TRD §7.1's
  // whole reason for existing) - this only ever differs in a test harness that overrides
  // it, so building off window.location covers the real cases without hardcoding a host.
  const base = API_BASE || window.location.origin;
  const url = new URL("/ws/v1/tenant/incidents", base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("ticket", ticket);
  return url.toString();
}

/** Live incident updates for the inbox. A ticket-per-connection (see the backend's own
 * `ws_tickets` module for why) means every reconnect - the first one, and every one after
 * a dropped connection - fetches a fresh one; nothing here ever reuses a ticket.
 *
 * Deliberately does not try to be the source of truth for what an incident looks like:
 * an event here only ever carries a type and an id, and the caller decides what to do
 * with that (in practice, `IncidentsPage` reloads its list) - keeping one place, the
 * REST GET, responsible for what a row actually renders. */
export function useIncidentSocket(onEvent: (event: IncidentEvent) => void): SocketStatus {
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let retryTimer: number | null = null;
    let attempt = 0;

    async function connect() {
      if (cancelled) return;
      setStatus("connecting");
      let ticket: string;
      try {
        const res = await apiFetch<TicketResponse>("/api/v1/tenant/realtime/ws-ticket", {
          method: "POST",
        });
        ticket = res.ticket;
      } catch {
        scheduleRetry();
        return;
      }
      if (cancelled) return;

      socket = new WebSocket(wsUrl(ticket));
      socket.onopen = () => {
        attempt = 0;
        if (!cancelled) setStatus("open");
      };
      socket.onmessage = (message) => {
        try {
          onEventRef.current(JSON.parse(message.data as string) as IncidentEvent);
        } catch {
          // A malformed frame is not a reason to drop the connection - skip it and
          // keep listening for the next one.
        }
      };
      socket.onclose = () => {
        if (cancelled) return;
        setStatus("closed");
        scheduleRetry();
      };
      socket.onerror = () => socket?.close();
    }

    function scheduleRetry() {
      if (cancelled) return;
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      attempt += 1;
      retryTimer = window.setTimeout(() => void connect(), delay);
    }

    void connect();

    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, []);

  return status;
}
