import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { tokenStore } from './tokenStore';
import * as authApi from '../api/auth';
import { ApiError, NetworkError } from '../api/client';

/** `checking` while the initial SecureStore check is still in flight - distinct from
 * `loggedIn`/`loggedOut`, so the router (`app/_layout.tsx`) doesn't redirect to /login
 * for a split second on every cold start before the real answer is known.
 *
 * A token is only ever persisted after a FULL successful login (see tokenStore.setSession
 * call sites - api/auth.ts's `login()` only for the (currently dead, see its own comment)
 * direct-authenticated case, and `verifyMfa()` once a challenge is satisfied). That cold-
 * start check can therefore only ever resolve to `loggedIn` or `loggedOut` - never
 * `mfaRequired`. An abandoned in-flight MFA challenge simply reverts to `loggedOut` on a
 * fresh cold start (matching the ticket's own 10-minute server-side TTL) - there is no
 * attempt to resume a half-finished login across a cold start, and that is deliberate
 * scope, not a gap. */
type AuthStage = 'checking' | 'loggedOut' | 'mfaRequired' | 'loggedIn';

interface MfaChallenge {
  ticket: string;
  factor: 'totp' | 'email_otp';
}

interface AuthContextValue {
  authStage: AuthStage;
  mfaChallenge: MfaChallenge | null;
  isLoggingIn: boolean;
  loginError: string | null;
  login: (email: string, password: string) => Promise<void>;
  verifyMfa: (input: { code?: string; recovery_code?: string }) => Promise<void>;
  cancelMfa: () => void;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [authStage, setAuthStage] = useState<AuthStage>('checking');
  const [mfaChallenge, setMfaChallenge] = useState<MfaChallenge | null>(null);
  const [isLoggingIn, setIsLoggingIn] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  // `isLoggingIn` only reflects reality after a re-render - a rapid double-tap on
  // "Verify"/"Sign in" fires a second call before that first render lands, since
  // `isLoggingIn` is still `false` from the caller's point of view. `login_mfa_tickets.py`
  // deliberately does NOT consume the ticket until success (so a mistyped code can be
  // retried), so two concurrent, both-correct `/login/mfa-verify` calls can both succeed,
  // both call `admit_device()`/`_issue_tokens()`, and silently orphan one of the two
  // sessions (last write to `mobile_devices.session_id` wins) - not an auth bypass, but a
  // real, unguarded race. This ref is checked and set synchronously, before either
  // function's first `await`, which a `useState` value cannot do.
  const submittingRef = useRef(false);

  useEffect(() => {
    tokenStore.isLoggedIn().then((loggedIn) => setAuthStage(loggedIn ? 'loggedIn' : 'loggedOut'));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setIsLoggingIn(true);
    setLoginError(null);
    try {
      const result = await authApi.login(email, password);
      if (result.status === 'mfa_required') {
        setMfaChallenge({ ticket: result.mfa_ticket!, factor: result.mfa_factor! });
        setAuthStage('mfaRequired');
      } else {
        // Tokens are already persisted inside authApi.login() itself for this case.
        setAuthStage('loggedIn');
      }
    } catch (error) {
      // "Invalid email or password" is the real, constant-shape message the backend
      // itself returns for both a wrong password and an unknown email (auth.py's own
      // user-enumeration defense) - shown verbatim, not reworded into something that
      // leaks more than the server itself chose to.
      if (error instanceof ApiError) {
        setLoginError(error.message);
      } else if (error instanceof NetworkError) {
        setLoginError(error.message);
      } else {
        setLoginError('Something went wrong. Please try again.');
      }
      throw error;
    } finally {
      submittingRef.current = false;
      setIsLoggingIn(false);
    }
  }, []);

  const verifyMfa = useCallback(
    async (input: { code?: string; recovery_code?: string }) => {
      if (!mfaChallenge) {
        throw new Error('verifyMfa called with no active MFA challenge');
      }
      if (submittingRef.current) return;
      submittingRef.current = true;
      setIsLoggingIn(true);
      setLoginError(null);
      try {
        await authApi.verifyMfa({ mfa_ticket: mfaChallenge.ticket, ...input });
        // Tokens are already persisted inside authApi.verifyMfa() itself.
        setMfaChallenge(null);
        setAuthStage('loggedIn');
      } catch (error) {
        if (error instanceof ApiError) {
          setLoginError(error.message);
        } else if (error instanceof NetworkError) {
          setLoginError(error.message);
        } else {
          setLoginError('Something went wrong. Please try again.');
        }
        throw error;
      } finally {
        submittingRef.current = false;
        setIsLoggingIn(false);
      }
    },
    [mfaChallenge],
  );

  const cancelMfa = useCallback(() => {
    setMfaChallenge(null);
    setLoginError(null);
    setAuthStage('loggedOut');
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout(); // best-effort server-side revoke; local session is always
    // cleared regardless (api/auth.ts's own logout docstring).
    setAuthStage('loggedOut');
  }, []);

  return (
    <AuthContext.Provider
      value={{ authStage, mfaChallenge, isLoggingIn, loginError, login, verifyMfa, cancelMfa, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within an AuthProvider');
  return context;
}
