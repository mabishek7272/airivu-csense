import { apiFetch } from "./client";

/** This tenant user's own TOTP MFA. Mirrors backend/tenant_api/app/api/mfa.py. */

const BASE = "/api/v1/tenant/auth/mfa";

export interface MfaStatus {
  enrolled: boolean;
  recovery_codes_remaining: number;
}

export interface EnrollResult {
  secret: string;
  otpauth_uri: string;
}

export interface ConfirmResult {
  recovery_codes: string[];
}

export interface VerifyResult {
  verified: boolean;
  used_recovery_code: boolean;
}

export function getMfaStatus() {
  return apiFetch<MfaStatus>(`${BASE}/status`);
}

export function enrollTotp() {
  return apiFetch<EnrollResult>(`${BASE}/totp/enroll`, { method: "POST" });
}

export function confirmTotp(code: string) {
  return apiFetch<ConfirmResult>(`${BASE}/totp/confirm`, {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

export function verifyMfa(input: { code?: string; recovery_code?: string }) {
  return apiFetch<VerifyResult>(`${BASE}/verify`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function removeTotp() {
  return apiFetch<void>(`${BASE}/totp`, { method: "DELETE" });
}
