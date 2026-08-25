"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/auth/AuthContext";

export default function IndexPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isLoading) return;
    router.replace(isAuthenticated ? "/dashboard" : "/login");
  }, [isAuthenticated, isLoading, router]);

  return null;
}
