"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { ApiRequestError } from "../api/client";

/** The states a view can be in, each with its own treatment.
 *
 *  Trimmed port of frontend/customer-crm/src/components/States.tsx - `LoadingRows`,
 *  `EmptyPanel`, `NoResultsPanel`, `ErrorPanel`, `FailureState`, `InlineSpinner` and the
 *  panels it dispatches to. Still dropped: `LoadingList`/`SlowNetworkNotice` - built for
 *  the CRM's detection-card/thumbnail layout and slow-network affordance, neither of
 *  which this app has yet. `InlineSpinner` was added back for the Settings page's MFA
 *  enroll/confirm flow - the CSS for it was already sitting unused in globals.css.
 *
 *  These are separated because collapsing them is how a UI lies to the person using it:
 *
 *    *Loading* shown as *empty* tells an operator there are no models when the request is
 *    still in flight. They stop looking.
 *
 *    *Offline* shown as *error* sends someone to check a server that is fine, when the
 *    actual problem is the network in the room they are standing in.
 *
 *    *No results* shown as *empty* tells someone the registry has nothing in it when the
 *    truth is their filter excludes everything. The fix is one click away and they cannot
 *    see it.
 *
 *    *Permission denied* shown as *not found* makes a person doubt the record exists
 *    rather than ask an administrator for access.
 */

/* -------------------------------------------------------------------------- Loading */

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

/** An in-progress action that isn't a whole-page load - confirming a code, saving a
 *  form field. Port of frontend/customer-crm/src/components/States.tsx's own component;
 *  see this file's own top comment for why it was re-added here. */
export function InlineSpinner({ label = "Working…" }: { label?: string }) {
  return (
    <span className="inline-spinner" role="status">
      <span className="spinner" aria-hidden="true" />
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

/* --------------------------------------------------------------------------- Empty */

/** Nothing exists yet. The call to action creates the first one - omit `action` when
 *  there is nothing this screen can offer someone to do about it (e.g. this app has no
 *  "upload a model" affordance). */
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

/** Things exist, but none match the current filter - which is a different problem with a
 *  different fix. Saying "no models" when the answer is "no models *matching this*" hides
 *  the one click that would show them. */
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
        This device has lost its network connection. Nothing is wrong with the platform -
        the page will recover on its own once you are back online.
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
      <p className="muted">Ask a platform administrator to grant it.</p>
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
        <Link className="btn" href="/login">
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
  // still claims to be online - a captive portal, DNS failure, or the server unreachable.
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
