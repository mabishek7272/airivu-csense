import { apiFetch } from "./client";

/** Platform organization listing and creation. Mirrors backend/admin_api/app/api/organizations.py. */

export interface Organization {
  id: string;
  display_name: string;
  organization_type: string;
  status: string;
  tenant_id: string | null;
}

export interface CreateOrganizationInput {
  organization_name: string;
  organization_type: "direct_customer" | "reseller";
  owner_email: string;
  owner_display_name: string;
}

export interface CreatedOrganization {
  organization_id: string;
  tenant_id: string;
  organization_type: string;
  display_name: string;
  owner_email: string;
  // Set only when the invitation email could not actually be sent.
  invitation_link: string | null;
}

const BASE = "/api/v1/admin/organizations";

export function listOrganizations() {
  return apiFetch<Organization[]>(BASE);
}

export function createOrganization(body: CreateOrganizationInput) {
  return apiFetch<CreatedOrganization>(BASE, { method: "POST", body: JSON.stringify(body) });
}
