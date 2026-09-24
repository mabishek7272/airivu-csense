import { apiFetch } from "./client";

/** People registered for face recognition. Mirrors `backend/tenant_api/app/api/faces.py`.
 *
 *  There is deliberately no field here that could carry a face template or a photo back
 *  out: the server never returns either. A photo is embedded and discarded on upload, and
 *  only an encrypted vector is kept. */

export type FaceListType = "known" | "watchlist";

export interface FaceIdentity {
  id: string;
  display_name: string;
  external_ref: string | null;
  list_type: FaceListType;
  status: "active" | "disabled";
  template_count: number;
  consent_recorded_at: string;
  consent_note: string;
  created_at: string;
}

export interface CreateFaceIdentityInput {
  display_name: string;
  external_ref?: string | null;
  list_type: FaceListType;
  consent: boolean;
  consent_note: string;
}

export function listFaceIdentities(): Promise<FaceIdentity[]> {
  return apiFetch<FaceIdentity[]>("/api/v1/tenant/faces/identities");
}

export function createFaceIdentity(input: CreateFaceIdentityInput): Promise<FaceIdentity> {
  return apiFetch<FaceIdentity>("/api/v1/tenant/faces/identities", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateFaceIdentity(
  id: string,
  patch: Partial<Pick<FaceIdentity, "display_name" | "external_ref" | "list_type" | "status">>,
): Promise<FaceIdentity> {
  return apiFetch<FaceIdentity>(`/api/v1/tenant/faces/identities/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteFaceIdentity(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/tenant/faces/identities/${id}`, { method: "DELETE" });
}

export function addFacePhotos(
  id: string,
  photos: File[],
): Promise<{ added: number; template_count: number }> {
  const body = new FormData();
  for (const photo of photos) body.append("photos", photo);
  return apiFetch(`/api/v1/tenant/faces/identities/${id}/photos`, { method: "POST", body });
}
