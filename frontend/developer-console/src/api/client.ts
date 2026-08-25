// Admin API client (platform audience). Same in-memory-access-token / httpOnly-refresh-
// cookie pattern as the Customer CRM client — see frontend/customer-crm/src/api/client.ts.
// The two are intentionally not shared code: Developer Console and Customer CRM are
// separate applications with separate origins, sessions, and token audiences (TRD §6).
"use client";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

let accessToken: string | null = null;
let expiresAt = 0;

export function setAccessToken(token: string, expiresInSeconds: number) {
  accessToken = token;
  expiresAt = Date.now() + expiresInSeconds * 1000 - 5000;
}

export function clearAccessToken() {
  accessToken = null;
  expiresAt = 0;
}

export function hasSession() {
  return accessToken !== null;
}

export async function refreshAccessToken(): Promise<boolean> {
  const res = await fetch(`${API_BASE}/api/v1/admin/auth/refresh`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    clearAccessToken();
    return false;
  }
  const data = await res.json();
  setAccessToken(data.access_token, data.expires_in);
  return true;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  correlation_id?: string;
  retryable: boolean;
}

export class ApiRequestError extends Error {
  status: number;
  body: ApiErrorBody;
  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.body = body;
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (accessToken && Date.now() > expiresAt) {
    await refreshAccessToken();
  }

  const doFetch = () =>
    fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...init.headers,
      },
    });

  let res = await doFetch();

  if (res.status === 401 && accessToken) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      res = await doFetch();
    }
  }

  if (!res.ok) {
    const body = (await res.json().catch(() => ({
      code: "unknown_error",
      message: res.statusText,
      retryable: false,
    }))) as ApiErrorBody;
    throw new ApiRequestError(res.status, body);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
