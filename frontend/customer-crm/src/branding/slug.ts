/** Detects a candidate brand slug from the first path segment, and nothing else — no
 *  network call, no React, safe to call before the app even mounts (`main.tsx` needs
 *  this synchronously to decide `<BrowserRouter basename>` before rendering anything).
 *
 *  Mirrors the backend's own reserved-word list (admin_api/app/api/branding.py's
 *  `_RESERVED_SLUGS`) so a real app route is never mistaken for a brand slug — kept as
 *  a manually-synced literal here for the same reason that file gives: different
 *  languages/packages, changes rarely enough that a build-time sync isn't worth it.
 */
const RESERVED_TOP_LEVEL_SEGMENTS = new Set([
  "accept-invitation", "audit", "cameras", "child-tenants", "dashboard",
  "detections", "edge", "incidents", "login", "notification-policies",
  "pipelines", "recipient-groups", "rules", "settings", "sites", "team",
  "webhooks", "zones", "api", "admin", "public", "ws", "media",
]);

/** Returns the candidate brand slug for the current URL, or null if the first path
 *  segment is empty or matches a real app route. Read once at module load — this app
 *  has no in-session brand-switching, so a stale value across a client-side navigation
 *  is not a real scenario (see BrandProvider's own docs for why). */
export function detectBrandSlug(pathname: string): string | null {
  const first = pathname.split("/").filter(Boolean)[0];
  if (!first) return null;
  if (RESERVED_TOP_LEVEL_SEGMENTS.has(first)) return null;
  return first;
}
