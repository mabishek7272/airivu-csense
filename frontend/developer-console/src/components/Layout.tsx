"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "../auth/AuthContext";

/** Port of frontend/customer-crm/src/components/Layout.tsx's shell, Next-native:
 *  react-router's `NavLink` (which sets `aria-current="page"` on the active route
 *  automatically) becomes `next/link`'s `Link` plus a manual `usePathname()` check - Next
 *  has no built-in active-link component. No tenant id shown here (this console has no
 *  tenant context - it's platform-scoped), unlike the CRM's header.
 */

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/models", label: "Models" },
];

export function Layout({ children }: { children: ReactNode }) {
  const { logout } = useAuth();
  const pathname = usePathname();

  return (
    <div className="app-shell">
      {/* Keyboard users should not have to tab through the whole nav on every page. */}
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="app-header">
        <div className="app-brand">
          AIRIVU <span>Developer Console</span>
        </div>
        <nav className="app-nav" aria-label="Main">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={pathname?.startsWith(item.href) ? "page" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <button type="button" onClick={logout}>
          Sign out
        </button>
      </header>

      <main className="app-main" id="main">
        {children}
      </main>
    </div>
  );
}
