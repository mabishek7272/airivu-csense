import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

/** A modal dialog, with the keyboard behaviour that makes one usable.
 *
 *  Most hand-rolled modals get three things wrong, and all three lock out keyboard users:
 *  focus is never moved into the dialog, Tab escapes to the page behind it, and Escape
 *  does nothing. This handles all three, and returns focus to whatever opened it on close
 *  so the user is not dumped at the top of the document.
 *
 *  Destructive confirmations name the thing being destroyed. "Are you sure?" is not a
 *  question anyone can answer correctly — "Delete camera Loading Bay 2?" is.
 */

export function Dialog({
  open,
  title,
  children,
  onClose,
  footer,
  labelledBy = "dialog-title",
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  labelledBy?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreFocusTo = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;

    restoreFocusTo.current = document.activeElement as HTMLElement | null;
    // Focus the first control rather than the panel, so the user lands on something they
    // can act on. Falls back to the panel when the dialog is text-only.
    const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    (focusable?.[0] ?? panelRef.current)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      // Trap Tab inside the panel. Without this, tabbing walks into the page behind the
      // overlay, where the user cannot see what is focused.
      const items = panelRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!items || items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    // The page behind must not scroll under the overlay.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restoreFocusTo.current?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="dialog-overlay"
      // Clicking the backdrop closes, but only the backdrop itself — a click that started
      // inside the panel and drifted out must not dismiss a half-filled form.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        ref={panelRef}
        tabIndex={-1}
      >
        <div className="dialog-header">
          <h2 id={labelledBy}>{title}</h2>
          <button type="button" className="toast-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer && <div className="dialog-footer">{footer}</div>}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Delete",
  destructive = true,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  destructive?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog
      open={open}
      title={title}
      onClose={busy ? () => undefined : onCancel}
      footer={
        <>
          {/* Cancel first in the DOM so it takes focus on open. For a destructive action
              the safe choice should be the one a hurried Enter press hits. */}
          <button type="button" className="btn-quiet" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={destructive ? "btn-danger" : ""}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </>
      }
    >
      {body}
    </Dialog>
  );
}
