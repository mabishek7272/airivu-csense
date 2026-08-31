import { apiRequest } from './client';
import { cookies } from '../auth/cookies';
import { tokenStore } from '../auth/tokenStore';
import type { AuthResponse, LoginRequest } from './types';

export async function login(email: string, password: string): Promise<void> {
  const body: LoginRequest = { email, password };
  const auth = await apiRequest<AuthResponse>('/api/v1/auth/login', { method: 'POST', body, auth: false });
  await tokenStore.setSession({
    accessToken: auth.access_token,
    expiresInSeconds: auth.expires_in,
    tenantId: auth.tenant_id,
  });
}

/** Best-effort real server-side revocation, but the *local* session is always cleared
 * regardless of whether that call succeeds - a user tapping "log out" while offline
 * must still actually be logged out on this device; the server-side session will
 * simply expire on its own if the revoke call never lands. */
export async function logout(): Promise<void> {
  try {
    const sessionId = await cookies.getSessionId();
    if (sessionId) {
      await apiRequest<void>(`/api/v1/auth/sessions/${sessionId}`, { method: 'DELETE' });
    }
  } catch {
    // Real server-side revoke is best-effort - see this function's own docstring.
  } finally {
    await tokenStore.clear();
    await cookies.clearAll();
  }
}
