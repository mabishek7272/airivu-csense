import { useAuth } from "../auth/AuthContext";

export function DashboardPage() {
  const { tenantId, logout } = useAuth();

  return (
    <main style={{ maxWidth: 720, margin: "5vh auto", fontFamily: "system-ui, sans-serif" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ fontSize: 20 }}>Dashboard</h1>
        <button onClick={logout}>Sign out</button>
      </header>
      <p style={{ color: "#555" }}>Tenant: {tenantId ?? "unknown"}</p>
      <section style={{ marginTop: 32, padding: 24, border: "1px dashed #ccc", borderRadius: 8 }}>
        <p>
          This is the Phase&nbsp;1 foundation shell. Sites, cameras, incidents, and reports land here in
          Phases&nbsp;2–6 per <code>CHECKLIST.md</code>.
        </p>
      </section>
    </main>
  );
}
