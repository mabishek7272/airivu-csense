import { API_BASE_URL, API_HOST_HEADER } from './config';
import { tokenStore } from '../auth/tokenStore';
import type { AuthResponse, ProblemResponse } from './types';

/**
 * Every real error this backend returns has one stable shape
 * (csense_shared.errors.ProblemResponse, docs/08_API_GUIDE.md's own "Error shape"
 * section) - this is that same contract, surfaced as a real Error subclass instead of
 * a screen having to re-parse a raw Response itself.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly httpStatusCode: number;
  readonly retryable: boolean;

  constructor(params: { code: string; httpStatusCode: number; retryable: boolean; message: string }) {
    super(params.message);
    this.name = 'ApiError';
    this.code = params.code;
    this.httpStatusCode = params.httpStatusCode;
    this.retryable = params.retryable;
  }
}

/** The request never reached the server, or no response came back at all - a real
 * connectivity problem, not something the server said. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

async function parseErrorBody(response: Response): Promise<ProblemResponse | null> {
  try {
    const body = await response.text();
    if (!body) return null;
    return JSON.parse(body) as ProblemResponse;
  } catch {
    return null;
  }
}

async function throwForResponse(response: Response): Promise<never> {
  const problem = await parseErrorBody(response);
  throw new ApiError({
    code: problem?.code ?? `http_${response.status}`,
    httpStatusCode: response.status,
    retryable: problem?.retryable ?? false,
    message: problem?.message ?? response.statusText,
  });
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Attach the bearer access token and retry once on a real 401 (via
   * `/api/v1/auth/refresh`). Auth endpoints themselves (login/refresh) pass
   * `auth: false` - they have no token yet, or ARE the refresh call, and retrying a
   * failed refresh through itself would be a real infinite loop. */
  auth?: boolean;
}

async function rawFetch(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Host: API_HOST_HEADER,
  };
  if (options.auth !== false) {
    const token = await tokenStore.getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  try {
    return await fetch(`${API_BASE_URL}${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      // credentials: 'include' is a fetch-spec concept aimed at cross-origin browser
      // requests; React Native's own fetch relies on the platform's native cookie jar
      // regardless (see src/auth/cookies.ts's own docstring for the full reasoning),
      // so this is deliberately omitted rather than cargo-culted from web code.
    });
  } catch {
    throw new NetworkError('Could not reach the server. Check your connection.');
  }
}

let refreshInFlight: Promise<boolean> | null = null;

/** Calls the real `/api/v1/auth/refresh` at most once concurrently - if three screens
 * all get a 401 at the same moment, they share one real refresh attempt rather than
 * racing three. */
async function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await rawFetch('/api/v1/auth/refresh', { method: 'POST', auth: false });
        if (!response.ok) return false;
        const auth = (await response.json()) as AuthResponse;
        await tokenStore.setSession({
          accessToken: auth.access_token,
          expiresInSeconds: auth.expires_in,
          tenantId: auth.tenant_id,
        });
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

/**
 * Every call in api/auth.ts and api/tenant.ts goes through this. A real 401 on an
 * `auth: true` (the default) call triggers exactly one real refresh attempt and one
 * retry - if that also fails, the session is genuinely gone (expired, revoked, or
 * already used elsewhere) and the caller sees the real 401, not an infinite loop.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const useAuth = options.auth !== false;
  let response = await rawFetch(path, options);

  if (response.status === 401 && useAuth) {
    const refreshed = await refreshSession();
    if (refreshed) {
      response = await rawFetch(path, options);
    } else {
      await tokenStore.clear();
    }
  }

  if (!response.ok) {
    await throwForResponse(response);
  }

  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
