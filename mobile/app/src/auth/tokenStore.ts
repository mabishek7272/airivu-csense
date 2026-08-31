import * as SecureStore from 'expo-secure-store';

/**
 * Holds the short-lived access token (and the tenant it was issued for), backed by
 * `expo-secure-store` - Keychain on iOS, EncryptedSharedPreferences/Keystore on
 * Android. The access token is a bearer credential good for `expires_in` seconds
 * against this tenant's data, and belongs in the same class of "never leaves the
 * device unencrypted" storage the backend's own `csense_shared.security.secret_store`
 * gives camera credentials, not `AsyncStorage` (unencrypted, disk-plain).
 *
 * The refresh token itself is never stored here - it never reaches this app as a value
 * to store at all (see api/types.ts's own note on AuthResponse). It lives in the
 * platform's own native cookie jar instead (src/auth/cookies.ts).
 */
const KEY_ACCESS_TOKEN = 'csense_access_token';
const KEY_EXPIRES_AT = 'csense_access_token_expires_at';
const KEY_TENANT_ID = 'csense_tenant_id';

export const tokenStore = {
  async getAccessToken(): Promise<string | null> {
    return SecureStore.getItemAsync(KEY_ACCESS_TOKEN);
  },

  async getTenantId(): Promise<string | null> {
    return SecureStore.getItemAsync(KEY_TENANT_ID);
  },

  /** Wall-clock expiry, computed once at issuance (now + expires_in) - a local
   * estimate used only to decide whether to proactively refresh, never trusted as the
   * actual authority. The server's own 401 (client.ts's own retry-once-on-401 logic) is
   * what's actually correct if this estimate and the server ever disagree, e.g. after
   * the device clock drifts. */
  async getAccessTokenExpiresAt(): Promise<number | null> {
    const raw = await SecureStore.getItemAsync(KEY_EXPIRES_AT);
    return raw ? Number(raw) : null;
  },

  async setSession(params: { accessToken: string; expiresInSeconds: number; tenantId: string | null }): Promise<void> {
    const expiresAt = Math.floor(Date.now() / 1000) + params.expiresInSeconds;
    await Promise.all([
      SecureStore.setItemAsync(KEY_ACCESS_TOKEN, params.accessToken),
      SecureStore.setItemAsync(KEY_EXPIRES_AT, String(expiresAt)),
      params.tenantId
        ? SecureStore.setItemAsync(KEY_TENANT_ID, params.tenantId)
        : SecureStore.deleteItemAsync(KEY_TENANT_ID),
    ]);
  },

  async isLoggedIn(): Promise<boolean> {
    const token = await this.getAccessToken();
    return !!token;
  },

  async clear(): Promise<void> {
    await Promise.all([
      SecureStore.deleteItemAsync(KEY_ACCESS_TOKEN),
      SecureStore.deleteItemAsync(KEY_EXPIRES_AT),
      SecureStore.deleteItemAsync(KEY_TENANT_ID),
    ]);
  },
};
