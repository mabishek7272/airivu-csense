import { apiFetch } from "./client";

/** This tenant's own effective license, entitlements, and live quota usage. Mirrors
 *  backend/tenant_api/app/api/license.py. `null` (not a 404) is the correct, common
 *  answer for a tenant nobody has issued a license to yet.
 */

export interface QuotaUsage {
  quota_code: string;
  limit_value: number;
  reserved_value: number;
  consumed_value: number;
}

export interface Entitlement {
  entitlement_code: string;
  value_type: "limit_numeric" | "boolean" | "json";
  limit_numeric: number | null;
  enabled_boolean: boolean | null;
  value_json: unknown;
}

export interface License {
  id: string;
  plan_code: string;
  plan_name: string;
  /** "scheduled" | "active" | "grace" | "suspended" | "expired" | "revoked" - grace and
   *  expiry are computed lazily server-side, so this always reflects the real current
   *  state as of the last time anything read it, not a stale snapshot. */
  status: string;
  starts_at: string;
  expires_at: string | null;
  grace_ends_at: string | null;
  entitlements: Entitlement[];
  quota_usage: QuotaUsage[];
}

export function getOwnLicense() {
  return apiFetch<License | null>("/api/v1/tenant/license");
}
