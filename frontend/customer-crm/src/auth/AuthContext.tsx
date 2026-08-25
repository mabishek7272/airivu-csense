import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { apiFetch, clearAccessToken, refreshAccessToken, setAccessToken } from "../api/client";

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  tenantId: string | null;
  login: (email: string, password: string) => Promise<void>;
  register: (organizationName: string, email: string, password: string, displayName: string) => Promise<void>;
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

  useEffect(() => {
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

  const logout = useCallback(() => {
    clearAccessToken();
    setIsAuthenticated(false);
    setTenantId(null);
  }, []);

  return (
    <AuthContext.Provider value={{ isAuthenticated, isLoading, tenantId, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
