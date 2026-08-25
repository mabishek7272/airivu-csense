"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { apiFetch, clearAccessToken, refreshAccessToken, setAccessToken } from "@/api/client";

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

interface AuthResponse {
  access_token: string;
  expires_in: number;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const ok = await refreshAccessToken();
      setIsAuthenticated(ok);
      setIsLoading(false);
    })();
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const data = await apiFetch<AuthResponse>("/api/v1/admin/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setAccessToken(data.access_token, data.expires_in);
    setIsAuthenticated(true);
  }, []);

  const logout = useCallback(() => {
    clearAccessToken();
    setIsAuthenticated(false);
  }, []);

  return <AuthContext.Provider value={{ isAuthenticated, isLoading, login, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
