"""Builds a SQL WHERE-clause fragment (+ bind params) expressing a TenantContext's own
site scope - the list-endpoint equivalent of TenantContext.can_access_site() for a single
resource. Every list endpoint that returns site-scoped rows (sites, cameras, zones,
incidents, rules) appends this fragment to its existing WHERE clause the same way it
already appends any other optional filter (see cameras.py's own `clauses`/`params`
pattern, which this is designed to slot into unchanged).

Deliberately NOT a new Postgres RLS policy: every RLS policy in this codebase enforces
tenant_id equality only (never an ANY() array-membership check - see CLAUDE.md/this
plan's own "Before you start" section) - site-level scoping is a within-tenant access
decision, the same layer every other permission check in this app already operates at,
not a second row-level-security boundary.
"""
from __future__ import annotations

from uuid import UUID

from csense_shared.security.tenant_context import TenantContext


def site_scope_sql_filter(context: TenantContext, *, column: str) -> tuple[str, dict[str, list[UUID]]]:
    """Returns (sql_fragment, params) for the given site_id column name (e.g. "site_id",
    "c.site_id"). The fragment is always a complete boolean expression - callers AND it
    into their own WHERE clause exactly like any other clause in their `clauses` list."""
    if context.site_scope_mode == "all":
        return "TRUE", {}
    if context.site_scope_mode == "selected" and context.site_ids:
        return f"{column} = ANY(:__site_scope_ids)", {"__site_scope_ids": list(context.site_ids)}
    # 'none', or 'selected' with an empty site_ids set (a real, reachable state - see
    # test_sql_filter_selected_scope_with_no_sites_returns_always_false).
    return "FALSE", {}
