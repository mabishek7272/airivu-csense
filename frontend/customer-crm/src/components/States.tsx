import type { ReactNode } from "react";

/** Loading, empty and error are distinct states and each needs its own treatment.
 *  Collapsing them — showing "no results" while a request is still in flight — is how a
 *  user concludes there are no incidents when in fact the API is down. */

export function LoadingList({ rows = 3 }: { rows?: number }) {
  return (
    <div className="card-list" aria-busy="true" aria-live="polite">
      <span className="visually-hidden">Loading…</span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="card detection-card" aria-hidden="true">
          <div className="thumb skeleton" />
          <div>
            <div className="skeleton" style={{ height: 18, width: "45%", marginBottom: 12 }} />
            <div className="skeleton" style={{ height: 12, width: "75%", marginBottom: 8 }} />
            <div className="skeleton" style={{ height: 12, width: "60%" }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function ErrorPanel({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-panel" role="alert">
      <strong>Could not load this view.</strong>
      <p style={{ margin: "8px 0 0" }}>{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} style={{ marginTop: 12 }}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyPanel({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card state-panel">
      <h2>{title}</h2>
      {children}
    </div>
  );
}
