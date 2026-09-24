"use client";

import { useEffect } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/auth/AuthContext";
import { listOrganizations } from "@/api/organizations";
import { useResource } from "@/hooks/useResource";
import { Layout } from "@/components/Layout";

/** Shared nav + tenant-name header for every `/tenants/[tenantId]/...` page - Sites,
 * Zones, Cameras, Edge, Rules, Pipelines all live under here, each managing one tenant's
 * operational setup on their behalf (see CHECKLIST.md's "move camera/site/zone/edge/
 * rules/pipeline management to the Console" entry). The tenant's own display name is
 * resolved from the existing org listing (`listOrganizations`) rather than a new
 * lookup-by-tenant-id endpoint - the same client-side match `dashboard/page.tsx`'s own
 * `orgName()` helper already does.
 */

const SUB_NAV = [
  { segment: "sites", label: "Sites" },
  { segment: "zones", label: "Zones" },
  { segment: "cameras", label: "Cameras" },
  { segment: "edge", label: "Edge" },
  { segment: "rules", label: "Rules" },
  { segment: "pipelines", label: "Pipelines" },
];

export default function TenantLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: { tenantId: string };
}) {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const orgs = useResource(listOrganizations, [isAuthenticated]);

  // Same guard every other protected page in this app applies (see dashboard/page.tsx) -
  // missing here meant this route rendered (and fetched, and failed) before the silent
  // token refresh on first load had a chance to finish.
  useEffect(() => {
    if (!isLoading && !isAuthenticated) router.replace("/login");
  }, [isAuthenticated, isLoading, router]);

  if (isLoading || !isAuthenticated) {
    return (
      <div className="auth-shell" aria-busy="true">
        <p>Loading…</p>
      </div>
    );
  }

  const org = orgs.data?.find((o) => o.tenant_id === params.tenantId);
  const tenantLabel = org?.display_name ?? `${params.tenantId.slice(0, 8)}…`;

  return (
    <Layout>
      <div className="page-header">
        <div>
          <p className="muted" style={{ marginBottom: 4 }}>
            <Link href="/dashboard">← All organizations</Link>
          </p>
          <h1>{tenantLabel}</h1>
        </div>
      </div>

      <nav className="app-nav" aria-label="Tenant infrastructure" style={{ marginBottom: 24 }}>
        {SUB_NAV.map((item) => {
          const href = `/tenants/${params.tenantId}/${item.segment}`;
          return (
            <Link key={item.segment} href={href} aria-current={pathname?.startsWith(href) ? "page" : undefined}>
              {item.label}
            </Link>
          );
        })}
      </nav>

      {children}
    </Layout>
  );
}
