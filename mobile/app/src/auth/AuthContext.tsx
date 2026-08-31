import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { tokenStore } from './tokenStore';
import * as authApi from '../api/auth';
import { ApiError, NetworkError } from '../api/client';

interface AuthContextValue {
  /** `null` while the initial SecureStore check is still in flight - distinct from
   * `false`, so the router (`app/_layout.tsx`) doesn't redirect to /login for a
   * split second on every cold start before the real answer is known. */
  isLoggedIn: boolean | null;
  isLoggingIn: boolean;
  loginError: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [isLoggedIn, setIsLoggedIn] = useState<boolean | null>(null);
  const [isLoggingIn, setIsLoggingIn] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  useEffect(() => {
    tokenStore.isLoggedIn().then(setIsLoggedIn);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    setIsLoggingIn(true);
    setLoginError(null);
    try {
      await authApi.login(email, password);
      setIsLoggedIn(true);
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
      setIsLoggingIn(false);
    }
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout(); // best-effort server-side revoke; local session is always
    // cleared regardless (api/auth.ts's own logout docstring).
    setIsLoggedIn(false);
  }, []);

  return (
    <AuthContext.Provider value={{ isLoggedIn, isLoggingIn, loginError, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within an AuthProvider');
  return context;
}
