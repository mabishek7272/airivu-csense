"use client";

import type { ReactNode } from "react";
import { AuthProvider } from "@/auth/AuthContext";
import { NotificationProvider } from "@/components/Notifications";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <NotificationProvider>{children}</NotificationProvider>
    </AuthProvider>
  );
}
