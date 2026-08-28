import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useOnlineStatus } from "../hooks/useNetwork";

/** The notification bar: transient messages about things the user just did.
 *
 *  Three rules it follows, each of which is a mistake this kind of component usually makes:
 *
 *  **Success auto-dismisses, failure does not.** A confirmation you missed costs nothing;
 *  an error you missed means you believe something saved when it did not. Errors stay
 *  until dismissed.
 *
 *  **It is announced, not just displayed.** Success uses `status` (polite — waits for a
 *  pause), errors use `alert` (assertive — interrupts). Getting this backwards either
 *  spams a screen reader on every keystroke or lets a failure pass silently.
 *
 *  **Dismiss timers pause on hover and focus.** A toast that vanishes while being read,
 *  or while the keyboard focus is inside it, is worse than no toast.
 *
 *  The offline banner lives here too, because it is the same shape of message and should
 *  never compete for the same space.
 */

export type ToastKind = "success" | "error" | "info" | "warning";

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  detail?: string;
  /** Left out for errors, which persist until dismissed. */
  durationMs?: number;
  action?: { label: string; onClick: () => void };
}

interface NotificationApi {
  notify: (toast: Omit<Toast, "id">) => number;
  success: (title: string, detail?: string) => number;
  error: (title: string, detail?: string) => number;
  dismiss: (id: number) => void;
}

const NotificationContext = createContext<NotificationApi | null>(null);

const DEFAULT_DURATION = 5000;

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const notify = useCallback((toast: Omit<Toast, "id">) => {
    const id = nextId.current++;
    setToasts((current) => {
      // Cap the stack. A loop that fires notifications should not be able to cover the
      // screen with them and hide the thing the user is trying to fix.
      const next = [...current, { ...toast, id }];
      return next.length > 4 ? next.slice(next.length - 4) : next;
    });
    return id;
  }, []);

  const api = useMemo<NotificationApi>(
    () => ({
      notify,
      dismiss,
      success: (title, detail) =>
        notify({ kind: "success", title, detail, durationMs: DEFAULT_DURATION }),
      // No duration: an error the user did not see is an error they think did not happen.
      error: (title, detail) => notify({ kind: "error", title, detail }),
    }),
    [notify, dismiss],
  );

  return (
    <NotificationContext.Provider value={api}>
      <ConnectionBanner />
      <ToastRegion toasts={toasts} onDismiss={dismiss} />
      {children}
    </NotificationContext.Provider>
  );
}

export function useNotifications(): NotificationApi {
  const context = useContext(NotificationContext);
  if (!context) {
    throw new Error("useNotifications must be used inside a NotificationProvider");
  }
  return context;
}

/** A persistent bar while the browser reports no connection.
 *
 *  Deliberately not a toast: this is a standing condition, not an event, and it should
 *  not be dismissible — dismissing it would not reconnect anything, and hiding it would
 *  leave someone wondering why every action fails.
 */
function ConnectionBanner() {
  const online = useOnlineStatus();
  const [everOffline, setEverOffline] = useState(false);

  useEffect(() => {
    if (!online) setEverOffline(true);
  }, [online]);

  if (online) {
    // Say so once, briefly, after a real outage — otherwise the user does not know
    // whether it is safe to retry.
    return everOffline ? <ReconnectedBanner onDone={() => setEverOffline(false)} /> : null;
  }

  return (
    <div className="top-banner top-banner-offline" role="alert">
      <span aria-hidden="true">⚠</span>
      <span>
        <strong>You are offline.</strong> Changes cannot be saved until the connection
        returns.
      </span>
    </div>
  );
}

function ReconnectedBanner({ onDone }: { onDone: () => void }) {
  useEffect(() => {
    const timer = window.setTimeout(onDone, 4000);
    return () => window.clearTimeout(timer);
  }, [onDone]);

  return (
    <div className="top-banner top-banner-online" role="status">
      <span aria-hidden="true">✓</span>
      <span>Back online.</span>
    </div>
  );
}

function ToastRegion({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-region">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: (id: number) => void }) {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (!toast.durationMs || paused) return;
    const timer = window.setTimeout(() => onDismiss(toast.id), toast.durationMs);
    return () => window.clearTimeout(timer);
  }, [toast.durationMs, toast.id, onDismiss, paused]);

  // Errors interrupt; everything else waits for a natural pause. Reversing these either
  // talks over the user constantly or lets a failure go unannounced.
  const assertive = toast.kind === "error" || toast.kind === "warning";

  return (
    <div
      className={`toast toast-${toast.kind}`}
      role={assertive ? "alert" : "status"}
      aria-live={assertive ? "assertive" : "polite"}
      // The timer pauses while the pointer or the keyboard is inside, so a toast cannot
      // disappear mid-read or while its action button has focus.
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span className="toast-icon" aria-hidden="true">
        {toast.kind === "success" ? "✓" : toast.kind === "error" ? "✕" : "!"}
      </span>
      <div className="toast-body">
        <strong>{toast.title}</strong>
        {toast.detail && <p>{toast.detail}</p>}
      </div>
      {toast.action && (
        <button type="button" className="btn-quiet" onClick={toast.action.onClick}>
          {toast.action.label}
        </button>
      )}
      <button
        type="button"
        className="toast-close"
        onClick={() => onDismiss(toast.id)}
        aria-label={`Dismiss: ${toast.title}`}
      >
        ✕
      </button>
    </div>
  );
}
