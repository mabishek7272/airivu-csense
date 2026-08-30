import { apiFetch } from "./client";

/** License plans and license issuance. Mirrors backend/admin_api/app/api/licensing.py.
 *  Issuing a license (POST /licenses) requires a recent step-up verification
 *  (TRD-SEC-010) - a 403 `step_up_required` from that call is not a bug, see
 *  components/IssueLicenseDialog.tsx for how the UI walks someone through it.
 */

export interface EntitlementSpec {
  value_type: "limit_numeric" | "boolean" | "json";
  limit_numeric?: number;
  enabled_boolean?: boolean;
  value_json?: unknown;
}

export interface LicensePlan {
  id: string;
  code: string;
  name: string;
  license_type: string;
  billing_period: string;
  default_entitlements: Record<string, EntitlementSpec>;
  status: string;
}

export interface CreateLicensePlanInput {
  code: string;
  name: string;
  license_type: string;
  billing_period: "quarterly" | "half_yearly" | "yearly";
  default_entitlements: Record<string, EntitlementSpec>;
}

export interface License {
  id: string;
  tenant_id: string;
  plan_code: string;
  /** "scheduled" | "active" | "grace" | "suspended" | "expired" | "revoked" - grace and
   *  expiry are computed lazily server-side (see csense_shared.licensing.lifecycle), so
   *  this always reflects the real current state as of the last time anything read it. */
  status: string;
  starts_at: string;
  expires_at: string | null;
  grace_ends_at: string | null;
  entitlements: Record<string, EntitlementSpec>;
}

export interface IssueLicenseInput {
  tenant_id: string;
  plan_code: string;
  expires_at?: string;
  /** Reduced-friction warning window past expires_at before the license becomes a hard
   *  stop. Defaults server-side (14 days) when omitted. */
  grace_days?: number;
  entitlement_overrides?: Record<string, EntitlementSpec>;
}

export interface RenewLicenseInput {
  expires_at?: string;
  grace_days?: number;
}

/** Extends an existing license's term and restores it to active - the way out of
 *  grace/expired/suspended (not revoked, a deliberate terminal state). Same step-up
 *  gate as issueLicense. No dedicated dialog yet (API-only this pass, same deferral
 *  shape support grants already used for its own missing UI) - callable directly once
 *  a renewal screen exists. */
export function renewLicense(licenseId: string, body: RenewLicenseInput) {
  return apiFetch<License>(`/api/v1/admin/licenses/${licenseId}/renew`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listLicensePlans() {
  return apiFetch<LicensePlan[]>("/api/v1/admin/license-plans");
}

export function createLicensePlan(body: CreateLicensePlanInput) {
  return apiFetch<LicensePlan>("/api/v1/admin/license-plans", { method: "POST", body: JSON.stringify(body) });
}

export function listLicenses(tenantId?: string) {
  const qs = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  return apiFetch<License[]>(`/api/v1/admin/licenses${qs}`);
}

export function issueLicense(body: IssueLicenseInput) {
  return apiFetch<License>("/api/v1/admin/licenses", { method: "POST", body: JSON.stringify(body) });
}
