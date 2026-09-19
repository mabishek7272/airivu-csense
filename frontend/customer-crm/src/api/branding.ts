import { apiFetch } from "./client";

/** Mirrors backend PublicBrandingOut (public_branding.py / branding.py) exactly — the
 *  two endpoints share one response shape since they answer the same question from
 *  different trust levels, not two different questions. */
export interface Branding {
  slug: string;
  display_name: string;
  logo_url: string | null;
  favicon_url: string | null;
  color_primary: string | null;
  color_accent: string | null;
  color_canvas: string | null;
}

/** Unauthenticated — works pre-login. 404 (thrown as ApiRequestError) means no branding
 *  exists for this slug; callers should fall back to the default CSense look. */
export function getPublicBranding(slug: string) {
  return apiFetch<Branding>(`/api/v1/public/branding/${encodeURIComponent(slug)}`);
}

/** Authenticated — the session's own organization's effective branding (own, or
 *  inherited from a reseller ancestor). `null` (not a 404) means no branding is
 *  configured anywhere in the chain — the normal case for a non-white-labeled tenant. */
export function getEffectiveBranding() {
  return apiFetch<Branding | null>("/api/v1/tenant/branding/effective");
}
