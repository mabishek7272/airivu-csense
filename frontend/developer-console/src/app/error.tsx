"use client";

import { useEffect } from "react";

/** Root error boundary for the whole app. Nothing like this existed before - a client-
 * side crash anywhere rendered as a blank white page in production, with zero indication
 * anything had gone wrong. This won't fix the underlying bug, but it turns "the page is
 * just blank" into an actual message someone can act on or report.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // eslint-disable-next-line no-console
    console.error(error);
  }, [error]);

  return (
    <div className="auth-shell">
      <div className="card auth-card" style={{ maxWidth: 480 }}>
        <h1>Something went wrong</h1>
        <p>This page hit an unexpected error and could not render.</p>
        {error.digest && (
          <p className="muted mono" style={{ fontSize: 12 }}>
            Reference: {error.digest}
          </p>
        )}
        <button type="button" onClick={reset} style={{ marginTop: 16 }}>
          Try again
        </button>
      </div>
    </div>
  );
}
