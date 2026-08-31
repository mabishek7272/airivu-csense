import CookieManager from '@preeternal/react-native-cookie-manager';
import { API_BASE_URL } from '../api/config';

/** The session cookie's own name - see backend/tenant_api/app/api/auth.py's
 * `SESSION_COOKIE_NAME` constant. */
const SESSION_COOKIE_NAME = 'csense_session';

/**
 * This app deliberately does NOT read or write `Set-Cookie` response headers in JS, and
 * does not manually attach a `Cookie` request header either. Both iOS (`NSURLSession`)
 * and Android (`OkHttp`, via React Native's networking layer) already store and resend
 * cookies transparently for `fetch()` requests through their own native cookie jars,
 * below the JS bridge - the same "no extra code needed" behavior a browser already
 * gives the web Customer CRM for this exact flow (`csense_session`/`csense_refresh`,
 * httpOnly, path-scoped to `/api/v1/auth` - see api/types.ts's own AuthResponse note).
 *
 * `@preeternal/react-native-cookie-manager` is used for exactly the one real thing
 * fetch's own transparent handling can't give JS: reading a *specific* cookie's current
 * *value* - needed for `DELETE /api/v1/auth/sessions/{sessionId}`, which needs the
 * session id in its own URL path, and that id is never returned in any JSON body (see
 * api/auth.ts's own `logout`).
 *
 * **Named honestly, not silently assumed**: this app has not been run on a real device
 * yet (see mobile/app/README.md's own "What's been verified" section) - the native
 * cookie jar behavior described above is real, standard, and widely relied on in
 * production React Native apps talking to cookie-session backends, but it has not been
 * exercised against this specific backend on a real device from this codebase. Confirm
 * login -> app restart -> an authenticated call still works, and that refresh-on-401
 * actually rotates the session, on a real device before trusting this in production.
 */
export const cookies = {
  async getSessionId(): Promise<string | null> {
    const refreshUrl = `${API_BASE_URL}/api/v1/auth/refresh`;
    const all = await CookieManager.get(refreshUrl);
    return all[SESSION_COOKIE_NAME]?.value ?? null;
  },

  async clearAll(): Promise<void> {
    await CookieManager.clearAll();
  },
};
