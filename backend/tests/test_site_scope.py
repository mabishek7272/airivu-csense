"""Pure unit tests for site-scope enforcement - no DB needed, these are plain functions
over a TenantContext and a SQL-fragment builder."""
from __future__ import annotations

import uuid

from csense_shared.security.site_scope import site_scope_sql_filter
from csense_shared.security.tenant_context import TenantContext


def _context(*, site_scope_mode: str, site_ids: frozenset[uuid.UUID] = frozenset()) -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), membership_id=uuid.uuid4(),
        token_audience="csense-customer", permissions=frozenset(),
        site_scope_mode=site_scope_mode, site_ids=site_ids,
    )


def test_all_scope_can_access_any_site():
    context = _context(site_scope_mode="all")
    assert context.can_access_site(uuid.uuid4()) is True


def test_none_scope_cannot_access_any_site():
    context = _context(site_scope_mode="none")
    assert context.can_access_site(uuid.uuid4()) is False


def test_selected_scope_only_allows_its_own_site_ids():
    allowed = uuid.uuid4()
    other = uuid.uuid4()
    context = _context(site_scope_mode="selected", site_ids=frozenset({allowed}))
    assert context.can_access_site(allowed) is True
    assert context.can_access_site(other) is False


def test_default_context_is_unrestricted():
    # The dataclass default (site_scope_mode="none") existed before this feature and is
    # exercised by any TenantContext built without explicit scope args - e.g. the
    # support-grant-elevated path in deps.py, which has no real membership row to read a
    # scope from at all. A platform developer using an approved support grant must not be
    # silently locked out of every site - that path is a deliberate, separate elevation
    # mechanism, not a real tenant membership with real scoping.
    context = TenantContext(
        tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), membership_id=None,
        token_audience="csense-platform", permissions=frozenset(),
    )
    assert context.site_scope_mode == "none"


def test_sql_filter_all_scope_returns_no_clause():
    context = _context(site_scope_mode="all")
    clause, params = site_scope_sql_filter(context, column="site_id")
    assert clause == "TRUE"
    assert params == {}


def test_sql_filter_none_scope_returns_always_false():
    context = _context(site_scope_mode="none")
    clause, params = site_scope_sql_filter(context, column="site_id")
    assert clause == "FALSE"
    assert params == {}


def test_sql_filter_selected_scope_returns_any_clause_with_real_ids():
    site_a = uuid.uuid4()
    site_b = uuid.uuid4()
    context = _context(site_scope_mode="selected", site_ids=frozenset({site_a, site_b}))
    clause, params = site_scope_sql_filter(context, column="c.site_id")
    assert clause == "c.site_id = ANY(:__site_scope_ids)"
    assert set(params["__site_scope_ids"]) == {site_a, site_b}


def test_sql_filter_selected_scope_with_no_sites_returns_always_false():
    # A 'selected' membership that has never actually been assigned a site (a real,
    # reachable state - an owner picks "Selected sites only" then closes the dialog
    # before checking any box) must see nothing, not everything - ANY() against an empty
    # array is already FALSE in Postgres, but this is asserted explicitly rather than
    # relying on that SQL behaviour silently doing the right thing.
    context = _context(site_scope_mode="selected", site_ids=frozenset())
    clause, params = site_scope_sql_filter(context, column="site_id")
    assert clause == "FALSE"
    assert params == {}
