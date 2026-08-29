import { apiFetch } from "./client";

/** Recipient groups and notification policies.
 *
 *  Two resources, edited together in practice: a policy's escalation step names groups by
 *  id, so the group screen and the policy screen are two views onto one configuration, not
 *  two unrelated features.
 */

export const CHANNELS = ["email", "whatsapp", "in_app", "sms", "web_push", "webhook"] as const;
export type Channel = (typeof CHANNELS)[number];

export const SEVERITIES = ["info", "low", "medium", "high", "critical"] as const;

export interface QuietHours {
  start: string;
  end: string;
  timezone: string;
}

export interface RecipientMember {
  id: string;
  recipient_group_id: string;
  user_id?: string | null;
  display_name?: string | null;
  email?: string | null;
  phone_e164?: string | null;
  channels: Channel[];
  active_schedule: QuietHours | null;
  status: string;
  created_at: string;
}

export interface RecipientMemberInput {
  user_id?: string;
  display_name?: string;
  email?: string;
  phone_e164?: string;
  channels: Channel[];
  active_schedule?: QuietHours | null;
}

export interface RecipientGroup {
  id: string;
  name: string;
  description?: string | null;
  status: string;
  member_count: number;
  created_at: string;
}

export interface RecipientGroupInput {
  name: string;
  description?: string;
}

const GROUPS = "/api/v1/tenant/notifications/recipient-groups";

export function listRecipientGroups() {
  return apiFetch<RecipientGroup[]>(GROUPS);
}

export function createRecipientGroup(body: RecipientGroupInput) {
  return apiFetch<RecipientGroup>(GROUPS, { method: "POST", body: JSON.stringify(body) });
}

export function updateRecipientGroup(id: string, body: Partial<RecipientGroupInput & { status: string }>) {
  return apiFetch<RecipientGroup>(`${GROUPS}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteRecipientGroup(id: string) {
  return apiFetch<void>(`${GROUPS}/${id}`, { method: "DELETE" });
}

export function listMembers(groupId: string) {
  return apiFetch<RecipientMember[]>(`${GROUPS}/${groupId}/members`);
}

export function addMember(groupId: string, body: RecipientMemberInput) {
  return apiFetch<RecipientMember>(`${GROUPS}/${groupId}/members`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateMember(
  groupId: string,
  memberId: string,
  body: Partial<RecipientMemberInput> & { clear_schedule?: boolean; status?: string },
) {
  return apiFetch<RecipientMember>(`${GROUPS}/${groupId}/members/${memberId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function removeMember(groupId: string, memberId: string) {
  return apiFetch<void>(`${GROUPS}/${groupId}/members/${memberId}`, { method: "DELETE" });
}

/* ---------------------------------------------------------------------- policies */

export interface EscalationStep {
  level: number;
  delay_seconds: number;
  channels: Channel[];
  recipient_group_ids: string[];
}

export interface PolicyVersion {
  version_number: number;
  steps: EscalationStep[];
  published_at: string;
}

export interface NotificationPolicy {
  id: string;
  name: string;
  severities: string[];
  type_codes: string[];
  status: string;
  active_version: PolicyVersion | null;
  created_at: string;
}

export interface PolicyInput {
  name: string;
  severities?: string[];
  type_codes?: string[];
}

const POLICIES = "/api/v1/tenant/notifications/policies";

export function listPolicies() {
  return apiFetch<NotificationPolicy[]>(POLICIES);
}

export function createPolicy(body: PolicyInput) {
  return apiFetch<NotificationPolicy>(POLICIES, { method: "POST", body: JSON.stringify(body) });
}

export function updatePolicy(
  id: string,
  body: Partial<PolicyInput & { status: string }>,
) {
  return apiFetch<NotificationPolicy>(`${POLICIES}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deletePolicy(id: string) {
  return apiFetch<void>(`${POLICIES}/${id}`, { method: "DELETE" });
}

export function publishVersion(id: string, steps: EscalationStep[]) {
  return apiFetch<NotificationPolicy>(`${POLICIES}/${id}/versions`, {
    method: "POST",
    body: JSON.stringify({ steps }),
  });
}
