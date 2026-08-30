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
  status: string;
  starts_at: string;
  expires_at: string | null;
  entitlements: Record<string, EntitlementSpec>;
}

export interface IssueLicenseInput {
  tenant_id: string;
  plan_code: string;
  expires_at?: string;
  entitlement_overrides?: Record<string, EntitlementSpec>;
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
