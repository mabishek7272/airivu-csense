import { useEffect, useRef, useState } from 'react';
import { apiRequest } from '../api/client';
import { API_BASE_URL, API_HOST_HEADER } from '../api/config';

export type SocketStatus = 'connecting' | 'open' | 'closed';

/**
 * Mirrors the real emitted shape from `_poll_and_forward`
 * (backend/tenant_api/app/api/realtime.py) exactly - confirmed by reading that function
 * directly rather than assumed from the plan. The plan's own sample guessed a payload
 * of `{incident_id}` alone; the real backend also sends `type` (the outbox event type)
 * and `occurred_at` (ISO timestamp) on every frame.
 */
export interface AlertEvent {
  type: string;
  incident_id: string;
  occurred_at: string;
}

interface TicketResponse {
  ticket: string;
  expires_in: number;
}

const MAX_BACKOFF_MS = 10000;

/**
 * React Native's real, installed `WebSocket` (node_modules/react-native/Libraries/
 * WebSocket/WebSocket.js) accepts a third `options: { headers?: Record<string,
 * string> }` argument, forwarded verbatim to the native module
 * (`NativeWebSocketModule.connect(url, protocols, {headers}, socketId)`) - confirmed
 * real and honored on both platforms by reading the native implementations directly:
 * ReactAndroid's `WebSocketModule.kt` adds each entry via OkHttp's
 * `Request.Builder.addHeader`, and iOS's `RCTWebSocketModule.mm` adds each entry to an
 * `NSMutableURLRequest`. TypeScript's own ambient `WebSocket` type comes from the `DOM`
 * lib (expo/tsconfig.base's `lib: ["DOM", "ESNext"]`), which only declares the
 * browser's 2-argument constructor - this narrow, local cast is the honest way to reach
 * the real, supported 3-argument RN constructor without widening `WebSocket`'s type
 * anywhere else in the app.
 */
type RNWebSocketCtor = new (
  url: string,
  protocols?: string | string[] | null,
  options?: { headers?: Record<string, string> },
) => WebSocket;
const RNWebSocket = WebSocket as unknown as RNWebSocketCtor;

function wsUrl(ticket: string): string {
  const base = API_BASE_URL.replace(/^http/, 'ws');
  const query = new URLSearchParams({ ticket }).toString();
  return `${base}/ws/v1/tenant/incidents?${query}`;
}

/**
 * A faithful React Native port of the web CRM's own `useIncidentSocket`
 * (frontend/customer-crm/src/hooks/useIncidentSocket.ts) onto RN's built-in
 * `WebSocket` global - same ticket-per-connection-attempt (every reconnect mints a
 * fresh ticket, never reuses one), `onEventRef` pattern (so `onEvent` can change
 * identity across renders without tearing down the socket), `SocketStatus` return, and
 * exponential backoff capped at `MAX_BACKOFF_MS` with a tracked `retryTimer` that IS
 * cleared on unmount - the web hook's own real behavior; the plan's own simplified
 * sample dropped this and could leak a duplicate reconnect loop after a fast
 * mount/unmount/remount.
 *
 * Named `useAlertsSocket` (this app's own Home/Alerts naming) rather than the web
 * hook's `useIncidentSocket`, but the wire event shape is identical - both connect to
 * the same backend socket.
 *
 * One real, deliberate departure from the web hook: an `enabled` gate. The web page is
 * always the incident inbox, so its hook connects unconditionally on mount. This app's
 * Settings -> Notifications screen has an explicit on/off toggle for the in-app socket
 * (a separate delivery path from push), and turning it off must not leave a connection
 * open in the background - so `enabled` is a real dependency of the effect, closing the
 * socket (and cancelling any pending retry) the moment it goes false, and reconnecting
 * fresh the moment it goes true again.
 */
export function useAlertsSocket(enabled: boolean, onEvent: (event: AlertEvent) => void): SocketStatus {
  const [status, setStatus] = useState<SocketStatus>('closed');
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!enabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStatus('closed');
      return;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    async function connect() {
      if (cancelled) return;
      setStatus('connecting');
      let ticket: string;
      try {
        const res = await apiRequest<TicketResponse>('/api/v1/tenant/realtime/ws-ticket', { method: 'POST' });
        ticket = res.ticket;
      } catch {
        scheduleRetry();
        return;
      }
      if (cancelled) return;

      // Also attempts the same `Host` header every REST call already sends
      // (client.ts's own `rawFetch`) - load-bearing for this app's local/dev stack,
      // where Traefik routes by `Host` (infra/traefik/dynamic.yml) and the literal
      // API_BASE_URL host alone ("localhost:8080" / "10.0.2.2:8080") wouldn't route
      // correctly without it. See this file's own module docstring above for why this
      // is a real, supported RN constructor argument, not a fabricated one.
      socket = new RNWebSocket(wsUrl(ticket), undefined, { headers: { Host: API_HOST_HEADER } });
      socket.onopen = () => {
        attempt = 0;
        if (!cancelled) setStatus('open');
      };
      socket.onmessage = (message) => {
        try {
          onEventRef.current(JSON.parse(message.data as string) as AlertEvent);
        } catch {
          // malformed frame - skip, keep listening
        }
      };
      socket.onclose = () => {
        if (cancelled) return;
        setStatus('closed');
        scheduleRetry();
      };
      socket.onerror = () => socket?.close();
    }

    function scheduleRetry() {
      if (cancelled) return;
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      attempt += 1;
      retryTimer = setTimeout(() => void connect(), delay);
    }

    void connect();
    return () => {
      cancelled = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      socket?.close();
    };
  }, [enabled]);

  return status;
}
