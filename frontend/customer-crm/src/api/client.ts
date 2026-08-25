// Tenant API client. The access token is kept in memory only (never localStorage) to
// limit XSS blast radius (TRD §7.1); the refresh token lives in an httpOnly cookie the
// browser sends automatically, so this module never touches it directly.

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

let accessToken: string | null = null;
let expiresAt = 0;

export function setAccessToken(token: string, expiresInSeconds: number) {
  accessToken = token;
  expiresAt = Date.now() + expiresInSeconds * 1000 - 5000; // refresh 5s early
}

export function clearAccessToken() {
  accessToken = null;
  expiresAt = 0;
}

export function hasSession() {
  return accessToken !== null;
}

async function refreshAccessToken(): Promise<boolean> {
  const res = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
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
  constructor(public status: number, public body: ApiErrorBody) {
    super(body.message);
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

  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export { API_BASE, refreshAccessToken };
