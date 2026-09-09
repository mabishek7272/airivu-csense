import { apiFetch } from "./client";

/** Webhook endpoints: tenant-configured HTTP receivers of CSense domain events.
 *
 *  Mirrors `backend/tenant_api/app/api/webhooks.py` exactly, including what it refuses to
 *  hand back. Neither the destination URL nor the signing secret is ever returned by a
 *  plain GET — `signing_secret` exists only on `CreateWebhookResult` and
 *  `RotateSecretResult`, the two responses that exist specifically to show it once. There
 *  is no field anywhere in this file that could carry either value back out of a list or
 *  patch response — the same "the type is the record of the decision, not just a
 *  reflection of it" discipline `cameras.ts`'s own docstring states for camera
 *  credentials. `url_host_display` is deliberately all a list ever gets of the URL: the
 *  hostname, never the path or query a receiver's own auth token might be embedded in.
 */

export interface WebhookEndpoint {
  id: string;
  name: string;
  url_host_display: string;
  event_filters: string[];
  status: string;
  created_at: string;
}

export interface CreateWebhookInput {
  name: string;
  url: string;
  event_filters: string[];
}

/** Extends the plain endpoint shape with the signing secret — present on this response
 *  only, shown once by the caller and never again. */
export interface CreateWebhookResult extends WebhookEndpoint {
  signing_secret: string;
}

export interface PatchWebhookInput {
  name?: string;
  event_filters?: string[];
  status?: "active" | "disabled";
}

export interface RotateSecretResult {
  signing_secret: string;
}

export interface TestDeliveryResult {
  delivered: boolean;
  response_status: number | null;
  response_time_ms: number | null;
  error: string | null;
}

export type WebhookDeliveryStatus = "pending" | "succeeded" | "failed" | "abandoned";

export interface WebhookDelivery {
  id: string;
  event_type: string;
  attempt_number: number;
  status: WebhookDeliveryStatus;
  scheduled_at: string;
  sent_at: string | null;
  response_status: number | null;
  response_time_ms: number | null;
  next_attempt_at: string | null;
  /** Already redacted server-side at write time — this client never redacts anything of
   *  its own, it just renders whatever came back. */
  failure_summary_redacted: string | null;
}

export interface WebhookDeliveryPage {
  items: WebhookDelivery[];
  next_cursor: string | null;
}

const BASE = "/api/v1/tenant/webhooks";

export function listWebhooks() {
  return apiFetch<WebhookEndpoint[]>(BASE);
}

export function createWebhook(body: CreateWebhookInput) {
  return apiFetch<CreateWebhookResult>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateWebhook(id: string, body: PatchWebhookInput) {
  return apiFetch<WebhookEndpoint>(`${BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteWebhook(id: string) {
  return apiFetch<void>(`${BASE}/${id}`, { method: "DELETE" });
}

export function rotateWebhookSecret(id: string) {
  return apiFetch<RotateSecretResult>(`${BASE}/${id}/rotate-secret`, { method: "POST" });
}

export function testWebhook(id: string) {
  return apiFetch<TestDeliveryResult>(`${BASE}/${id}/test`, { method: "POST" });
}

export function listWebhookDeliveries(
  id: string,
  params: { status?: string; cursor?: string; limit?: number } = {},
) {
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return apiFetch<WebhookDeliveryPage>(`${BASE}/${id}/deliveries${qs ? `?${qs}` : ""}`);
}
