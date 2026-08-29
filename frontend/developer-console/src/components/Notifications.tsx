"use client";

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

/** The toast bar: transient messages about things the user just did.
 *
 *  Trimmed port of frontend/customer-crm/src/components/Notifications.tsx - toasts only.
 *  The CRM's offline `ConnectionBanner` is left out here: nothing in this feature needs
 *  it yet, and the top-banner styling wasn't ported to this app's stylesheet. Port both
 *  together if a future page needs it.
 *
 *  **Success auto-dismisses, failure does not.** A confirmation you missed costs nothing;
 *  an error you missed means you believe something saved when it did not.
 *
 *  **It is announced, not just displayed.** Success uses `status` (polite), errors use
 *  `alert` (assertive) - getting this backwards either spams a screen reader or lets a
 *  failure pass silently.
 *
 *  **Dismiss timers pause on hover and focus.** A toast that vanishes while being read is
 *  worse than no toast.
 */

export type ToastKind = "success" | "error" | "info" | "warning";

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  detail?: string;
  /** Left out for errors, which persist until dismissed. */
  durationMs?: number;
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
