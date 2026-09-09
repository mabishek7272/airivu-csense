import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { apiFetch, clearAccessToken, refreshAccessToken, setAccessToken } from "../api/client";

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  tenantId: string | null;
  login: (email: string, password: string) => Promise<void>;
  register: (organizationName: string, email: string, password: string, displayName: string) => Promise<void>;
  acceptInvitation: (token: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

interface AuthResponse {
  access_token: string;
  expires_in: number;
  tenant_id: string | null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [tenantId, setTenantId] = useState<string | null>(null);

  // React.StrictMode (main.tsx) double-invokes effects in development to surface
  // exactly this kind of non-idempotent effect: without this guard, mount fires the
  // silent refresh twice with the same refresh-token cookie, and the second call reuses
  // an already-rotated token. This guard is good hygiene independent of that dev-mode
  // symptom too — it avoids a genuinely wasted duplicate network call on every real page
  // load in production. It does NOT fix two separate real browser tabs racing the same
  // refresh concurrently; that's a real backend concern, handled by a grace window in
  // `rotate_session` (backend/shared/csense_shared/security/sessions.py).
  const hasStartedSilentRefresh = useRef(false);

  useEffect(() => {
    if (hasStartedSilentRefresh.current) return;
    hasStartedSilentRefresh.current = true;

    // On load, attempt a silent refresh using the httpOnly cookie — this is what
    // survives a page reload, since the access token itself is memory-only.
    (async () => {
      const ok = await refreshAccessToken();
      setIsAuthenticated(ok);
      setIsLoading(false);
    })();
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const data = await apiFetch<AuthResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setAccessToken(data.access_token, data.expires_in);
    setTenantId(data.tenant_id);
    setIsAuthenticated(true);
  }, []);

  const register = useCallback(
    async (organizationName: string, email: string, password: string, displayName: string) => {
      const data = await apiFetch<AuthResponse>("/api/v1/auth/register", {
        method: "POST",
        body: JSON.stringify({
          organization_name: organizationName,
          email,
          password,
          display_name: displayName,
        }),
      });
      setAccessToken(data.access_token, data.expires_in);
      setTenantId(data.tenant_id);
      setIsAuthenticated(true);
    },
    [],
  );

  const acceptInvitation = useCallback(async (token: string, password: string) => {
    const data = await apiFetch<AuthResponse>("/api/v1/auth/accept-invitation", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    });
    setAccessToken(data.access_token, data.expires_in);
    setTenantId(data.tenant_id);
    setIsAuthenticated(true);
  }, []);

  const logout = useCallback(() => {
    clearAccessToken();
    setIsAuthenticated(false);
    setTenantId(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{ isAuthenticated, isLoading, tenantId, login, register, acceptInvitation, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
