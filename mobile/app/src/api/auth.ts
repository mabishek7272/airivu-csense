import { Platform } from 'react-native';
import { apiRequest } from './client';
import { deviceName, getDeviceId } from '../auth/device';
import { cookies } from '../auth/cookies';
import { tokenStore } from '../auth/tokenStore';
import type { LoginRequest, LoginResult } from './types';

/**
 * The backend's own `/login` ALWAYS returns `mfa_required` (either a TOTP-enrolled
 * user's authenticator or a freshly emailed one-time code - see `LoginResult`'s own
 * docstring in api/types.ts) - there is no `authenticated` case here, only from
 * `verifyMfa()` below. Tokens are therefore only ever persisted from THIS function when
 * (hypothetically) the backend shape changes to allow it; today that branch is dead but
 * kept so this function stays correct against the real, documented contract rather than
 * only against today's backend behaviour.
 */
export async function login(email: string, password: string): Promise<LoginResult> {
  const body: LoginRequest = {
    email,
    password,
    client: 'mobile',
    device_id: await getDeviceId(),
    device_name: deviceName(),
    platform: Platform.OS,
  };
  const result = await apiRequest<LoginResult>('/api/v1/auth/login', { method: 'POST', body, auth: false });
  if (result.status === 'authenticated') {
    await tokenStore.setSession({
      accessToken: result.access_token!,
      expiresInSeconds: result.expires_in!,
      tenantId: result.tenant_id ?? null,
    });
  }
  return result;
}

/** Completes a login-time MFA challenge started by `login()` above - a 6-digit TOTP/
 * email code, or (TOTP-enrolled accounts only) a recovery code. Always resolves to
 * `status: 'authenticated'` on success (a second `mfa_required` here would mean the
 * backend itself is broken), so tokens are unconditionally persisted. */
export async function verifyMfa(input: { mfa_ticket: string; code?: string; recovery_code?: string }): Promise<LoginResult> {
  const result = await apiRequest<LoginResult>('/api/v1/auth/login/mfa-verify', { method: 'POST', body: input, auth: false });
  await tokenStore.setSession({
    accessToken: result.access_token!,
    expiresInSeconds: result.expires_in!,
    tenantId: result.tenant_id ?? null,
  });
  return result;
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
