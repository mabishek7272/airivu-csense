import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiRequestError } from "../api/client";

/** The states a view can be in, each with its own treatment.
 *
 *  These are separated because collapsing them is how a UI lies to the person using it:
 *
 *    *Loading* shown as *empty* tells an operator there are no incidents when the request
 *    is still in flight. They stop looking. That is the worst failure in this product.
 *
 *    *Offline* shown as *error* sends someone to check a server that is fine, when the
 *    actual problem is the wifi in the room they are standing in.
 *
 *    *No results* shown as *empty* tells someone their site has no cameras when in truth
 *    their filter excludes all of them. The fix is one click away and they cannot see it.
 *
 *    *Permission denied* shown as *not found* makes a person doubt the record exists
 *    rather than ask an administrator for access.
 *
 *  Every panel here is a live region or an alert as appropriate, because a screen-reader
 *  user gets no benefit from a state that only reads as a visual change.
 */

/* -------------------------------------------------------------------------- Loading */

export function LoadingList({ rows = 3 }: { rows?: number }) {
  return (
    <div className="card-list" aria-busy="true" aria-live="polite">
      <span className="visually-hidden">Loading…</span>
      {Array.from({ length: rows }, (_, i) => (
        // Skeletons are decorative: they convey "content is coming", which the live
        // region above already says. Exposing them would read as gibberish.
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

export function LoadingRows({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div className="card" aria-busy="true" aria-live="polite">
      <span className="visually-hidden">Loading…</span>
      <table className="data-table">
        <tbody aria-hidden="true">
          {Array.from({ length: rows }, (_, r) => (
            <tr key={r}>
              {Array.from({ length: columns }, (_, c) => (
                <td key={c}>
                  <div className="skeleton" style={{ height: 14, width: `${90 - c * 15}%` }} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function InlineSpinner({ label = "Working…" }: { label?: string }) {
  return (
    <span className="inline-spinner" role="status">
      <span className="spinner" aria-hidden="true" />
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

/** Added to a loading view when it has been slow, never substituted for it.
 *
 *  The request may still succeed. Replacing the spinner with an error at four seconds
 *  throws away a response that arrives at five; this only answers "is it stuck?". */
export function SlowNetworkNotice({ onCancel }: { onCancel?: () => void }) {
  return (
    <div className="notice notice-info" role="status">
      <span className="spinner" aria-hidden="true" />
      <div>
        <strong>This is taking longer than usual.</strong>
        <p>
          Still waiting on the server. Your connection may be slow — the page will update
          on its own if it comes through.
        </p>
      </div>
      {onCancel && (
        <button type="button" className="btn-quiet" onClick={onCancel}>
          Stop waiting
        </button>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------------- Empty */

/** Nothing exists yet. The call to action creates the first one. */
export function EmptyPanel({
  title,
  children,
  action,
  icon = "○",
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  icon?: string;
}) {
  return (
    <div className="card state-panel">
      <div className="state-icon" aria-hidden="true">
        {icon}
      </div>
      <h2>{title}</h2>
      {children}
      {action && <div className="state-actions">{action}</div>}
    </div>
  );
}

/** Things exist, but none match the current filter — which is a different problem with a
 *  different fix. Saying "no cameras" when the answer is "no cameras *matching this*"
 *  hides the one click that would show them. */
export function NoResultsPanel({
  query,
  onClear,
  entity = "results",
}: {
  query?: string;
  onClear?: () => void;
  entity?: string;
}) {
  return (
    <div className="card state-panel">
      <div className="state-icon" aria-hidden="true">
        ⌕
      </div>
      <h2>No {entity} match your filters</h2>
      <p>
        {query ? (
          <>
            Nothing matched <strong>“{query}”</strong>. Check the spelling, or widen the
            filters.
          </>
        ) : (
          <>The filters you have applied exclude everything. Try widening them.</>
        )}
      </p>
      {onClear && (
        <div className="state-actions">
          <button type="button" onClick={onClear}>
            Clear all filters
          </button>
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------------- Error */

export function OfflinePanel({ onRetry }: { onRetry?: () => void }) {
  return (
    <div className="card state-panel state-offline" role="alert">
      <div className="state-icon" aria-hidden="true">
        ⚠
      </div>
      <h2>You are offline</h2>
      <p>
        This device has lost its network connection. Nothing is wrong with CSense — the
        page will recover on its own once you are back online.
      </p>
      {onRetry && (
        <div className="state-actions">
          <button type="button" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
    </div>
  );
}

export function PermissionDeniedPanel({ permission }: { permission?: string }) {
  return (
    <div className="card state-panel" role="alert">
      <div className="state-icon" aria-hidden="true">
        ⃠
      </div>
      <h2>You do not have access to this</h2>
      <p>
        Your account is signed in correctly, but your role does not include this.
        {permission && (
          <>
            {" "}
            It needs the <code className="mono">{permission}</code> permission.
          </>
        )}
      </p>
      {/* Named explicitly so the person knows who to ask, rather than concluding the
          record does not exist and giving up. */}
      <p className="muted">Ask an administrator on your account to grant it.</p>
    </div>
  );
}

export function SessionExpiredPanel() {
  return (
    <div className="card state-panel" role="alert">
      <div className="state-icon" aria-hidden="true">
        ⏱
      </div>
      <h2>Your session has expired</h2>
      <p>
        You have been signed out for security after a period of inactivity. Signing in
        again will bring you back to this page.
      </p>
      <div className="state-actions">
        <Link className="btn" to="/login">
          Sign in again
        </Link>
      </div>
    </div>
  );
}

export function NotFoundPanel({ entity = "page" }: { entity?: string }) {
  return (
    <div className="card state-panel">
      <div className="state-icon" aria-hidden="true">
        ?
      </div>
      <h2>That {entity} does not exist</h2>
      <p>It may have been deleted, or the link may be wrong.</p>
    </div>
  );
}

export function ErrorPanel({
  message,
  onRetry,
  correlationId,
}: {
  message: string;
  onRetry?: () => void;
  correlationId?: string;
}) {
  return (
    <div className="error-panel" role="alert">
      <strong>Could not load this view.</strong>
      <p style={{ margin: "8px 0 0" }}>{message}</p>
      {/* Support cannot find anything in the logs without this, and the user cannot be
          expected to reproduce the failure on demand. */}
      {correlationId && (
        <p className="muted mono" style={{ margin: "8px 0 0", fontSize: 12 }}>
          Reference: {correlationId}
        </p>
      )}
      {onRetry && (
        <button type="button" onClick={onRetry} style={{ marginTop: 12 }}>
          Try again
        </button>
      )}
    </div>
  );
}

/** Chooses the right panel for a failure, so every page does not re-derive it.
 *
 *  This is the whole argument of this module in one function: the same rejected promise
 *  means four different things to the person looking at it, and the status code is what
 *  tells them apart.
 */
export function FailureState({
  error,
  online,
  onRetry,
  entity,
}: {
  error: unknown;
  online: boolean;
  onRetry?: () => void;
  entity?: string;
}) {
  if (!online) return <OfflinePanel onRetry={onRetry} />;

  if (error instanceof ApiRequestError) {
    if (error.status === 401) return <SessionExpiredPanel />;
    if (error.status === 403) {
      const permission =
        typeof error.body.details?.required_permission === "string"
          ? error.body.details.required_permission
          : undefined;
      return <PermissionDeniedPanel permission={permission} />;
    }
    if (error.status === 404) return <NotFoundPanel entity={entity} />;
    return (
      <ErrorPanel
        message={error.body.message}
        correlationId={error.body.correlation_id}
        onRetry={error.body.retryable === false ? undefined : onRetry}
      />
    );
  }

  // A fetch that rejects without a response is usually the network, even when the browser
  // still claims to be online — a captive portal, DNS failure, or the server unreachable.
  return (
    <ErrorPanel
      message={
        error instanceof Error
          ? error.message
          : "Something went wrong and the reason was not reported."
      }
      onRetry={onRetry}
    />
  );
}
