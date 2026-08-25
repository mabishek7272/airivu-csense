"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";

interface Organization {
  id: string;
  display_name: string;
  organization_type: string;
  status: string;
}

export default function DashboardPage() {
  const { isAuthenticated, isLoading, logout } = useAuth();
  const router = useRouter();
  const [organizations, setOrganizations] = useState<Organization[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!isAuthenticated) return;
    apiFetch<Organization[]>("/api/v1/admin/organizations")
      .then(setOrganizations)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load organizations"));
  }, [isAuthenticated]);

  if (isLoading || !isAuthenticated) return <p style={{ textAlign: "center", marginTop: "20vh" }}>Loading…</p>;

  return (
    <main style={{ maxWidth: 720, margin: "5vh auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ fontSize: 20 }}>Platform dashboard</h1>
        <button onClick={logout}>Sign out</button>
      </header>

      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16 }}>Organizations</h2>
        {error && <p style={{ color: "#b00020" }}>{error}</p>}
        {!error && !organizations && <p>Loading organizations…</p>}
        {organizations && organizations.length === 0 && <p style={{ color: "#555" }}>No organizations yet.</p>}
        {organizations && organizations.length > 0 && (
          <ul>
            {organizations.map((org) => (
              <li key={org.id}>
                {org.display_name} — {org.organization_type} ({org.status})
              </li>
            ))}
          </ul>
        )}
      </section>

      <section style={{ marginTop: 32, padding: 24, border: "1px dashed #ccc", borderRadius: 8 }}>
        <p>
          This is the Phase&nbsp;1 foundation shell. License/reseller management, model/pipeline registry, and audit
          search land here in Phases&nbsp;2–6 per <code>CHECKLIST.md</code>.
        </p>
      </section>
    </main>
  );
}
