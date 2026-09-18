# Licensing, RBAC & Reseller Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship four independent, previously-deferred features: (B) two new fixed tenant
roles for finer permission granularity, (A) real enforcement of per-site membership
scoping plus a picker UI, (C) an admin path to change an existing tenant's license plan
without touching the database by hand, and (D) a reseller aggregate rollup view across a
reseller's child tenants.

**Architecture:** All four features extend existing, already-shipped schema and
patterns — no feature here needs a new subsystem. Feature B is pure reference-data seeding
(new `roles`/`role_permissions` rows, the exact idiom migrations 0002/0010/0037 already
use). Feature A activates dormant schema (`memberships.site_scope_mode`,
`membership_resource_scopes`) that has existed since migration 0001 but was never read at
request time — the JWT gains two new claims, `TenantContext` gains a real
`can_access_site()` check, and five list endpoints gain a shared SQL filter. Feature C
follows `issue_license`'s own exact shape (same table writes, same step-up gate, same
audit pattern), refactored so plan-issuance logic is shared between "first license" and
"change plan" rather than duplicated. Feature D follows the `edge_vpn_pool_snapshot()` /
`support_grant_lookup()` SECURITY DEFINER precedent (migrations 0029/0051) to cross the
RLS boundary in a narrow, audited, parameterized way — the same boundary that silently
emptied a naive query once already in this codebase's own history
(`reseller.py`'s child-tenant list).

**Tech Stack:** FastAPI + SQLAlchemy async (raw `text()` queries, matching every existing
endpoint in these files) on PostgreSQL with row-level security; Alembic migrations;
React + TypeScript (customer-crm and developer-console, Vite); pytest +
pytest-asyncio for unit-level coverage; standalone `scripts/e2e_*.py` for full
real-stack verification, matching this codebase's own established split (endpoint-level
features in this repo are verified almost entirely via e2e scripts, not endpoint unit
tests — `memberships.py`, `reseller.py`, and `licensing.py` have none today either).

---

## Before you start: real facts this plan depends on

These were confirmed by reading the actual code, not assumed — re-verify with a quick
`grep` if the codebase has moved since this was written (dated 2026-09-18):

- Latest migration on disk is `0053_diagnostic_read_permission.py`. New migrations in
  this plan are `0054`, `0055`, `0056`.
- Permission codes already exist for everything Feature B needs to grant (no new
  `permissions` rows required): `site.read`, `zone.read`, `camera.read`,
  `camera.create`, `camera.view_live`, `camera.manage`, `camera.probe`, `rule.read`,
  `rule.manage`, `incident.read`, `incident.acknowledge`, `incident.assign`,
  `audit.read`. (Note: an earlier draft of this plan omitted `camera.create` from
  `tenant_operator`'s grant list below, which caused a real 403 on `POST
  /api/v1/tenant/cameras` when Feature B's own e2e script ran for the first time -
  `camera.create` and `camera.manage` are genuinely distinct permission codes
  (migrations 0010 and 0021), and "add/reconfigure cameras" needs both. Fixed
  directly in migration 0054 before it shipped to any real membership; the code
  block below already reflects the corrected 13-permission grant.)
- `TenantContext` (`backend/shared/csense_shared/security/tenant_context.py`) already
  has `site_scope_mode: str = "none"` and `site_ids: frozenset[UUID] = frozenset()`
  fields — they've just never been populated with real values or read by anything.
  Feature A finishes wiring them; it does not add them.
- `memberships.site_scope_mode` is a 3-value enum: `all` / `selected` / `none`.
  **`none` means "sees no sites"** — confirmed by the customer-crm invite form's own
  option label (`TeamPage.tsx`), `"No sites (assign later)"`, which is the strongest
  signal of the feature's original intent, stronger than the enum's own bare name. This
  matters: turning on real enforcement is not a no-op for existing data. See Feature A
  Task 1 for the backfill this requires.
- The RLS pattern used everywhere in this codebase is strict tenant-id equality
  (`tenant_id = current_setting('app.tenant_id', true)::uuid`), never an `ANY()`
  membership check. Feature A's site-scoping is therefore enforced in the application
  layer (SQL `WHERE` clauses built per-request from `TenantContext`), not as a new RLS
  policy — consistent with how this schema already separates "which tenant" (RLS) from
  "which resource inside the tenant" (every existing permission check).

---

## File Structure

**Feature B — finer role granularity**
- Create: `backend/migrations/versions/0054_operator_and_viewer_roles.py`
- Modify: `backend/tenant_api/app/api/memberships.py` (role-name regex, both call sites)
- Modify: `frontend/customer-crm/src/api/memberships.ts` (role union type)
- Modify: `frontend/customer-crm/src/pages/TeamPage.tsx` (role `<select>` options)
- Create: `scripts/e2e_finer_roles.py`

**Feature A — per-site membership scoping**
- Create: `backend/migrations/versions/0055_site_scope_backfill_and_lookup.py`
- Modify: `backend/shared/csense_shared/security/tenant_context.py`
  (`can_access_site()`)
- Create: `backend/shared/csense_shared/security/site_scope.py` (SQL filter helper)
- Modify: `backend/tenant_api/app/repositories/identity.py` (`ActiveMembership` gains
  scope fields; `create_invited_membership` unchanged, already takes
  `site_scope_mode`)
- Modify: `backend/tenant_api/app/api/auth.py` (all 4 token-issuance call sites)
- Modify: `backend/tenant_api/app/deps.py` (`current_tenant_context` populates the new
  claims)
- Modify: `backend/shared/csense_shared/security/tokens.py` (`issue_access_token`/
  `decode_access_token` carry `ssm`/`sids` claims)
- Modify: `backend/tenant_api/app/api/memberships.py` (`InviteIn`/`MembershipPatchIn`
  accept `selected` + `site_ids`; write `membership_resource_scopes` rows)
- Modify: `backend/tenant_api/app/api/sites.py` (`list_sites`, `get_site`)
- Modify: `backend/tenant_api/app/api/cameras.py` (`list_cameras`, `load_camera`)
- Modify: `backend/tenant_api/app/api/zones.py` (`list_zones`)
- Modify: `backend/tenant_api/app/api/incidents.py` (`list_incidents`)
- Modify: `backend/tenant_api/app/api/rules.py` (`list_rules`)
- Test: `backend/tests/test_site_scope.py` (new, pure unit tests for the filter helper
  and `can_access_site`)
- Modify: `frontend/customer-crm/src/api/memberships.ts` (`site_ids`, `"selected"`)
- Modify: `frontend/customer-crm/src/pages/TeamPage.tsx` (site multi-select picker)
- Create: `scripts/e2e_site_scoping.py`

**Feature C — admin-initiated license plan change**
- Modify: `backend/admin_api/app/api/licensing.py` (extract `_provision_entitlements`,
  add `POST /api/v1/admin/licenses/{license_id}/change-plan`)
- Modify: `frontend/developer-console/src/api/licensing.ts` (`changeLicensePlan`)
- Create: `frontend/developer-console/src/components/ChangeLicensePlanDialog.tsx`
- Modify: `frontend/developer-console/src/app/dashboard/page.tsx` ("Change plan"
  button + dialog wiring)
- Create: `scripts/e2e_license_change_plan.py`

**Feature D — reseller aggregate rollup**
- Create: `backend/migrations/versions/0056_reseller_rollup.py`
- Modify: `backend/tenant_api/app/api/reseller.py` (`GET .../rollup`)
- Create: `frontend/customer-crm/src/api/reseller.ts`
- Create: `frontend/customer-crm/src/pages/ResellerRollupPage.tsx`
- Modify: `frontend/customer-crm/src/App.tsx` (route registration — verify exact
  routing file at implementation time; if a different router config file exists, wire
  the route there instead)
- Create: `scripts/e2e_reseller_rollup.py`

---

# Feature B: Finer role granularity

**Design decision (yours to build against, already made):** two new fixed system roles,
slotting into a clean permission hierarchy between the two that exist today:

```
tenant_viewer  ⊂  tenant_member  ⊂  tenant_operator  ⊂  tenant_owner
(read-only)       (current: read       (adds camera/rule    (full control:
                   + incident triage)   management)           users/settings/
                                                                security policy)
```

- **`tenant_viewer`** — strictly read-only across the tenant. Grants: `site.read`,
  `zone.read`, `camera.read`, `rule.read`, `incident.read`, `audit.read`. Cannot
  acknowledge/assign incidents, cannot manage anything. For a stakeholder who needs
  visibility (a manager, an auditor) without operational access.
- **`tenant_operator`** — day-to-day operations. Grants everything `tenant_member`
  already has (`incident.read`, `incident.acknowledge`, `incident.assign`, `site.read`,
  `camera.read`) plus `zone.read`, `rule.read`, `rule.manage`, `camera.view_live`,
  `camera.create`, `camera.manage`, `camera.probe`. Can run the day-to-day system — add/reconfigure
  cameras, tune detection rules, watch live view, work the incident queue — but cannot
  invite/manage users (`membership.manage`), change tenant settings
  (`tenant.settings.manage`), or touch security policy (`security.policy.manage`); those
  stay owner-only, matching the existing owner/member split's own stated reasoning
  (`0002`'s docstring: "Full control of a tenant, including user and settings
  management").

Existing `tenant_owner`/`tenant_member` roles and their grants are untouched.

### Task 1: Migration — seed the two new roles

**Files:**
- Create: `backend/migrations/versions/0054_operator_and_viewer_roles.py`

- [ ] **Step 1: Write the migration**

```python
"""Adds two fixed tenant roles between the existing tenant_member/tenant_owner pair:
tenant_viewer (strictly read-only) and tenant_operator (day-to-day operations - camera
and rule management, incident triage - without user/settings/security-policy control).

No new permission codes: every grant below is an existing permissions row (site.read,
zone.read, camera.read, camera.create, camera.view_live, camera.manage, camera.probe,
rule.read, rule.manage, incident.read, incident.acknowledge, incident.assign,
audit.read - see migrations 0002/0010 and the camera/zone/rule permission migrations
for where each was first added). This migration only adds `roles` + `role_permissions`
rows, following the exact idempotent pattern 0002/0010/0037 already established.

The resulting hierarchy: tenant_viewer (read-only) is a strict subset of tenant_member
(adds incident triage), which is a strict subset of tenant_operator (adds camera/rule
management), which is a strict subset of tenant_owner (adds user/settings/security-policy
management, per 0002's own "Full control of a tenant, including user and settings
management" description of that role).

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

# name, description, permission codes granted (all must already exist in `permissions`)
_NEW_ROLES: list[tuple[str, str, list[str]]] = [
    (
        "tenant_viewer",
        "Strictly read-only access across the tenant - no operational or management actions.",
        ["site.read", "zone.read", "camera.read", "rule.read", "incident.read", "audit.read"],
    ),
    (
        "tenant_operator",
        "Day-to-day operations: camera and rule management, live view, incident triage - "
        "without user, settings, or security-policy control.",
        [
            "site.read", "zone.read", "camera.read", "camera.create", "camera.view_live",
            "camera.manage", "camera.probe", "rule.read", "rule.manage", "incident.read",
            "incident.acknowledge", "incident.assign", "audit.read",
        ],
    ),
]


def _grant(bind, role_name: str, codes: list[str]) -> None:
    # Mirrors 0010's own `_grant` helper exactly - each migration re-defines this rather
    # than importing a previous migration file, since migration files are frozen history
    # and must not depend on each other's Python.
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = :name AND audience = 'customer'"),
        {"name": role_name},
    ).scalar_one_or_none()
    if role_id is None:
        return
    for code in codes:
        permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE code = :code"), {"code": code}
        ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id, effect) "
                "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
            ),
            {"r": role_id, "p": permission_id},
        )


def upgrade() -> None:
    bind = op.get_bind()

    for name, description, _codes in _NEW_ROLES:
        bind.execute(
            sa.text(
                "INSERT INTO roles (id, tenant_id, name, role_type, audience, description) "
                "VALUES (gen_random_uuid(), NULL, :name, 'system', 'customer', :description) "
                "ON CONFLICT DO NOTHING"
            ),
            {"name": name, "description": description},
        )

    for name, _description, codes in _NEW_ROLES:
        _grant(bind, name, codes)


def downgrade() -> None:
    names = tuple(name for name, *_ in _NEW_ROLES)
    op.execute(
        f"DELETE FROM role_permissions WHERE role_id IN "
        f"(SELECT id FROM roles WHERE tenant_id IS NULL AND name IN {names})"
    )
    op.execute(f"DELETE FROM roles WHERE tenant_id IS NULL AND name IN {names}")
```

- [ ] **Step 2: Run the migration against the live stack**

```bash
cd infra && docker compose --env-file ../.env build migrate && \
docker compose --env-file ../.env run --rm migrate
```
(this repo runs migrations via a dedicated one-shot `migrate` service, built from
`backend/migrations/Dockerfile` and COPYing the migrations directory at build time, not
a volume mount — a `build` before `run --rm` is required so the new migration file is
actually inside the image; confirmed the first time this plan's Task 1 ran, see that
task's own real output for the exact discovery.)
Expected: no errors; last line shows `0054` applied.

- [ ] **Step 3: Verify for real against live Postgres**

```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT r.name, count(*) FROM roles r JOIN role_permissions rp ON rp.role_id = r.id \
   WHERE r.name IN ('tenant_viewer','tenant_operator') GROUP BY r.name ORDER BY r.name"
```
Expected output (order may vary):
```
tenant_operator|13
tenant_viewer|6
```
If either count is 0 or wrong, a permission code was misspelled (the `_grant` helper's
`scalar_one()` would have raised `NoResultFound` at migration time instead if the code
didn't exist at all — a 0 count with no error means the role lookup itself returned
`None`, i.e. the role insert didn't take; check for a typo in `_NEW_ROLES`).

- [ ] **Step 4: Commit**

```bash
git add backend/migrations/versions/0054_operator_and_viewer_roles.py
git commit -m "rbac: add tenant_viewer and tenant_operator roles (finer granularity)"
```

### Task 2: Loosen the role-name validators in memberships.py

**Files:**
- Modify: `backend/tenant_api/app/api/memberships.py:90` (`InviteIn.role_name`)
- Modify: `backend/tenant_api/app/api/memberships.py:196` (`MembershipPatchIn.role_name`)

- [ ] **Step 1: Change both patterns**

In `InviteIn` (around line 90):
```python
    role_name: str = Field(pattern="^(tenant_owner|tenant_member)$")
```
becomes:
```python
    role_name: str = Field(pattern="^(tenant_owner|tenant_operator|tenant_member|tenant_viewer)$")
```

In `MembershipPatchIn` (around line 196):
```python
    role_name: str | None = Field(default=None, pattern="^(tenant_owner|tenant_member)$")
```
becomes:
```python
    role_name: str | None = Field(
        default=None, pattern="^(tenant_owner|tenant_operator|tenant_member|tenant_viewer)$"
    )
```

- [ ] **Step 2: Restart the tenant-api container and verify the new role is invitable for real**

```bash
cd infra && docker compose --env-file ../.env restart tenant-api
```

This is exercised end-to-end by Task 4's e2e script below rather than a standalone curl
here — invite requires a real tenant/owner session to call it against.

- [ ] **Step 3: Commit**

```bash
git add backend/tenant_api/app/api/memberships.py
git commit -m "rbac: accept tenant_operator and tenant_viewer in invite/patch role_name"
```

### Task 3: Frontend — offer the two new roles in the invite/edit UI

**Files:**
- Modify: `frontend/customer-crm/src/api/memberships.ts:5-33`
- Modify: `frontend/customer-crm/src/pages/TeamPage.tsx:225-303`

- [ ] **Step 1: Widen the TypeScript role union**

In `frontend/customer-crm/src/api/memberships.ts`, every occurrence of
`"tenant_owner" | "tenant_member"` (lines 9, 19, 30) becomes:
```typescript
  role_name: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
```
(apply to all three: `Membership.role_name`, `InviteInput.role_name`,
`MembershipPatch.role_name`).

- [ ] **Step 2: Add both options to the invite dialog's role `<select>`**

In `frontend/customer-crm/src/pages/TeamPage.tsx`, the `InviteDialog` component:

```typescript
  const [roleName, setRoleName] = useState<
    "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer"
  >("tenant_member");
```
(was `useState<"tenant_owner" | "tenant_member">("tenant_member")`).

```tsx
        <label>
          Role
          <select
            value={roleName}
            onChange={(e) =>
              setRoleName(
                e.target.value as "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer",
              )
            }
          >
            <option value="tenant_viewer">Viewer (read-only)</option>
            <option value="tenant_member">Member</option>
            <option value="tenant_operator">Operator (cameras, rules, incidents)</option>
            <option value="tenant_owner">Owner</option>
          </select>
        </label>
```
(was the 2-option `<select>` at lines 266-271).

- [ ] **Step 3: Confirm the build compiles**

```bash
cd frontend/customer-crm && npm run build
```
Expected: no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/customer-crm/src/api/memberships.ts frontend/customer-crm/src/pages/TeamPage.tsx
git commit -m "customer-crm: offer tenant_operator/tenant_viewer in the invite dialog"
```

### Task 4: Verify for real — e2e script

**Files:**
- Create: `scripts/e2e_finer_roles.py`

- [ ] **Step 1: Write the script**

```python
"""End-to-end verification of the two new fixed roles (tenant_viewer, tenant_operator) -
migration 0054. Proves against the real running stack:

  1. Register a real tenant (owner gets tenant_owner automatically).
  2. Invite a real member as tenant_viewer and one as tenant_operator through the real
     POST /api/v1/tenant/memberships.
  3. Read each invitation's Redis-backed token directly and accept it for real through
     POST /api/v1/auth/accept-invitation, exactly like a real invitee would.
  4. Log in as each new user and inspect their real JWT's "perm" claim - confirm
     tenant_viewer has read-only permissions and lacks incident.acknowledge/camera.manage,
     confirm tenant_operator has camera.manage/rule.manage/incident.acknowledge and lacks
     membership.manage/tenant.settings.manage.
  5. Confirm the real permission boundary at the HTTP layer, not just the claim: the
     viewer's token gets a real 403 from POST /api/v1/tenant/cameras (camera.manage
     required); the operator's token succeeds creating a real camera.

Run from the repo root with the stack up:
    python scripts/e2e_finer_roles.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:8080"
PASSWORD = "E2EFinerRoles!Password123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def redis_get_invitation_link(email: str) -> str:
    # Invitation tokens live in Redis, keyed by a value the create/consume ticket
    # functions control internally - the simplest real way to get it back out for a
    # script (not a test double) is the same one scripts/e2e_memberships.py already
    # uses: scan Redis for the one key referencing this email.
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "--scan", "--pattern", "invite:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    for key in result.stdout.strip().splitlines():
        value = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis", "redis-cli", "get", key],
            cwd="infra", capture_output=True, text=True, check=True,
        ).stdout.strip()
        if email in value:
            return key.split("invite:", 1)[1]
    raise RuntimeError(f"No invitation token found in Redis for {email}")


def decode_jwt_claims(token: str) -> dict:
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a real tenant")
    owner_email = f"roles-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Finer Roles E2E {suffix}",
        "email": owner_email, "password": PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {
        "name": "Depot", "code": f"depot-{suffix}",
    }, owner_token, expect=(201,))
    site_id = site["id"]

    step(2, "Invite one tenant_viewer and one tenant_operator through the real API")
    viewer_email = f"viewer-{suffix}@example.com"
    operator_email = f"operator-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": viewer_email, "display_name": "Viewer", "role_name": "tenant_viewer",
    }, owner_token, expect=(201,))
    api("/api/v1/tenant/memberships", {
        "email": operator_email, "display_name": "Operator", "role_name": "tenant_operator",
    }, owner_token, expect=(201,))
    print(f"    invited viewer={viewer_email} operator={operator_email}")

    step(3, "Accept both invitations for real, using the real Redis-backed token")
    viewer_ticket = redis_get_invitation_link(viewer_email)
    operator_ticket = redis_get_invitation_link(operator_email)
    api("/api/v1/auth/accept-invitation", {"token": viewer_ticket, "password": PASSWORD}, expect=(200,))
    api("/api/v1/auth/accept-invitation", {"token": operator_ticket, "password": PASSWORD}, expect=(200,))

    step(4, "Log in as each and inspect the real JWT perm claim")
    _, viewer_auth = api("/api/v1/auth/login", {"email": viewer_email, "password": PASSWORD})
    _, operator_auth = api("/api/v1/auth/login", {"email": operator_email, "password": PASSWORD})
    viewer_token = viewer_auth["access_token"]
    operator_token = operator_auth["access_token"]

    viewer_perms = set(decode_jwt_claims(viewer_token)["perm"])
    operator_perms = set(decode_jwt_claims(operator_token)["perm"])

    check("incident.read" in viewer_perms and "camera.read" in viewer_perms,
          "tenant_viewer's real token carries read permissions", failures)
    check("incident.acknowledge" not in viewer_perms and "camera.manage" not in viewer_perms,
          "tenant_viewer's real token has no operational permissions", failures)
    check("camera.manage" in operator_perms and "camera.create" in operator_perms and "rule.manage" in operator_perms
          and "incident.acknowledge" in operator_perms,
          "tenant_operator's real token carries operational permissions", failures)
    check("membership.manage" not in operator_perms and "tenant.settings.manage" not in operator_perms,
          "tenant_operator's real token has no user/settings-management permissions", failures)

    step(5, "Confirm the real 403/201 boundary at the HTTP layer, not just the claim")
    status, body = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Should Fail", "code": f"viewer-cam-{suffix}",
    }, viewer_token, expect=(201, 403))
    check(status == 403, f"tenant_viewer's real camera-create call is refused (got {status})", failures)

    status, body = api("/api/v1/tenant/cameras", {
        "site_id": site_id, "name": "Operator Cam", "code": f"operator-cam-{suffix}",
    }, operator_token, expect=(201, 403))
    check(status == 201, f"tenant_operator's real camera-create call succeeds (got {status})", failures)

    step(6, "Clean up")
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    for email in (owner_email, viewer_email, operator_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - tenant_viewer and tenant_operator verified for real, at both the JWT and HTTP layers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against the live stack**

```bash
python scripts/e2e_finer_roles.py
```
Expected: `PASS` with every check `ok`.

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e_finer_roles.py
git commit -m "e2e: verify tenant_viewer/tenant_operator against the real stack"
```

---

# Feature A: Per-site membership scoping

**The decision that shapes everything below** (see "Before you start" above): `none`
means "sees no sites." Enforcing this for real is a genuine behavior change for any
membership already sitting at `none` in the database — today, with zero enforcement,
those memberships can see every site in the tenant. Task 1 backfills every currently
*active* `none`-mode membership to `all` before enforcement code ships, so nothing that
works today silently breaks the moment this deploys. Only *new* invitations default to
real `none` (real lockout, matching the UI's own stated "assign later" intent) from this
point forward.

### Task 1: Migration — backfill existing scope + extend the login lookup function

**Files:**
- Create: `backend/migrations/versions/0055_site_scope_backfill_and_lookup.py`

- [ ] **Step 1: Write the migration**

```python
"""Two things, both required before site-scope enforcement can ship safely:

1. Data backfill: every currently-*active* membership with site_scope_mode='none' is set
   to 'all'. Today, with zero enforcement anywhere, a 'none'-scoped active membership can
   in practice see every site in its tenant - the enum value has never actually meant
   "sees nothing" for anyone using the product so far (see the invite form's own
   "No sites (assign later)" label for the intended, but until-now unenforced, meaning).
   Flipping enforcement on without this backfill would silently lock out every such
   membership the instant this deploys. Only 'invited'/'suspended'/'revoked' memberships
   are left alone - they have no current visibility to preserve, and a fresh invite from
   this point forward gets the real, enforced 'none' default (real lockout until a site is
   assigned), matching the UI's original intent for new invitations specifically.

2. csense_active_membership_for_user() (migration 0005) is extended to also return
   site_scope_mode and a site_ids array - it's the SECURITY DEFINER function login()/
   refresh() call before any tenant scope is set, so it's the only place that can resolve
   a user's own site scope pre-auth (the same reasoning 0005's own docstring gives for why
   it exists as SECURITY DEFINER at all: an ordinary RLS-scoped query can't run yet,
   there's no tenant context to scope it to). site_ids reads membership_resource_scopes
   where resource_type='site' and effect='allow' - the only kind of row this feature ever
   writes to that table (a narrower building block than the schema allows; 'deny' rows and
   non-site resource_types are out of scope for this pass, matching the plan's stated File
   Structure boundary).

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE memberships SET site_scope_mode = 'all', updated_at = now() "
        "WHERE site_scope_mode = 'none' AND status = 'active'"
    )

    # DROP + CREATE (not CREATE OR REPLACE) because the return type is changing - Postgres
    # refuses to REPLACE a function with a different RETURNS TABLE shape.
    op.execute("DROP FUNCTION IF EXISTS csense_active_membership_for_user(uuid)")
    op.execute(
        """
        CREATE FUNCTION csense_active_membership_for_user(p_user_id uuid)
        RETURNS TABLE (
            membership_id uuid,
            tenant_id uuid,
            role_id uuid,
            site_scope_mode text,
            site_ids uuid[]
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        STABLE
        AS $$
            SELECT
                m.id,
                m.tenant_id,
                m.role_id,
                m.site_scope_mode::text,
                COALESCE(
                    (
                        SELECT array_agg(mrs.resource_id)
                        FROM membership_resource_scopes mrs
                        WHERE mrs.membership_id = m.id
                          AND mrs.resource_type = 'site'
                          AND mrs.effect = 'allow'
                    ),
                    ARRAY[]::uuid[]
                )
            FROM memberships m
            WHERE m.user_id = p_user_id AND m.status = 'active'
            ORDER BY m.created_at
            LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION csense_active_membership_for_user(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION csense_active_membership_for_user(uuid) TO "csense_api"')


def downgrade() -> None:
    # The data backfill is not reversible (which specific rows were 'none' before upgrade
    # is not recorded) - downgrade restores the function shape only, matching how this
    # codebase's other backfill-carrying migrations already treat downgrade as
    # best-effort schema reversal, not a full data-state undo.
    op.execute("DROP FUNCTION IF EXISTS csense_active_membership_for_user(uuid)")
    op.execute(
        """
        CREATE FUNCTION csense_active_membership_for_user(p_user_id uuid)
        RETURNS TABLE (membership_id uuid, tenant_id uuid, role_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        STABLE
        AS $$
            SELECT m.id, m.tenant_id, m.role_id
            FROM memberships m
            WHERE m.user_id = p_user_id AND m.status = 'active'
            ORDER BY m.created_at
            LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION csense_active_membership_for_user(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION csense_active_membership_for_user(uuid) TO "csense_api"')
```

- [ ] **Step 2: Check the actual data being backfilled, before running the migration**

```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT count(*) FROM memberships WHERE site_scope_mode = 'none' AND status = 'active'"
```
Note the number printed — Step 4 confirms it drops to 0.

- [ ] **Step 3: Run the migration**

```bash
cd infra && docker compose --env-file ../.env build migrate && \
docker compose --env-file ../.env run --rm migrate
```
(this repo runs migrations via a dedicated one-shot `migrate` service, built from
`backend/migrations/Dockerfile` and COPYing the migrations directory at build time, not
a volume mount — a `build` before `run --rm` is required so the new migration file is
actually inside the image; confirmed the first time this plan's Task 1 ran, see that
task's own real output for the exact discovery.)

- [ ] **Step 4: Verify the backfill and the new function, for real**

```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT count(*) FROM memberships WHERE site_scope_mode = 'none' AND status = 'active'"
```
Expected: `0`.

```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT proname FROM pg_proc WHERE proname = 'csense_active_membership_for_user'"
```
Expected: `csense_active_membership_for_user` (one row — confirms the DROP+CREATE
succeeded rather than leaving two overloads).

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0055_site_scope_backfill_and_lookup.py
git commit -m "membership: backfill none->all scope, extend active-membership lookup with site scope"
```

### Task 2: `TenantContext.can_access_site()` + the SQL filter helper

**Files:**
- Modify: `backend/shared/csense_shared/security/tenant_context.py`
- Create: `backend/shared/csense_shared/security/site_scope.py`
- Test: `backend/tests/test_site_scope.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to confirm real failures (the module doesn't exist yet)**

```bash
cd backend && python3 -m pytest tests/test_site_scope.py -v
```
Expected: `ModuleNotFoundError: No module named 'csense_shared.security.site_scope'` and
`TypeError` for `can_access_site` not existing on `TenantContext`.

- [ ] **Step 3: Add `can_access_site()` to `TenantContext`**

In `backend/shared/csense_shared/security/tenant_context.py`, add a method to the
existing `TenantContext` dataclass (after `has_permission`, before the class ends):

```python
    def can_access_site(self, site_id: UUID) -> bool:
        # 'all'/'none' are unambiguous; 'selected' is the only mode that consults
        # site_ids at all. See site_scope.py's site_scope_sql_filter for the equivalent
        # check expressed as a SQL WHERE fragment, used for list endpoints instead of a
        # per-row Python check.
        if self.site_scope_mode == "all":
            return True
        if self.site_scope_mode == "selected":
            return site_id in self.site_ids
        return False
```

- [ ] **Step 4: Write `site_scope.py`**

```python
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
```

- [ ] **Step 5: Run the tests again**

```bash
cd backend && python3 -m pytest tests/test_site_scope.py -v
```
Expected: all pass (8 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/shared/csense_shared/security/tenant_context.py \
        backend/shared/csense_shared/security/site_scope.py \
        backend/tests/test_site_scope.py
git commit -m "security: add TenantContext.can_access_site() and the site-scope SQL filter helper"
```

### Task 3: Wire the JWT claims and `TenantContext` construction

**Files:**
- Modify: `backend/shared/csense_shared/security/tokens.py`
- Modify: `backend/tenant_api/app/repositories/identity.py`
- Modify: `backend/tenant_api/app/api/auth.py`
- Modify: `backend/tenant_api/app/deps.py`

- [ ] **Step 1: Add `ssm`/`sids` claims to `issue_access_token`/`decode_access_token`**

In `backend/shared/csense_shared/security/tokens.py`:

```python
@dataclass(frozen=True)
class AccessTokenClaims:
    subject_user_id: UUID
    audience: str
    tenant_id: UUID | None
    membership_id: UUID | None
    permissions: frozenset[str]
    site_scope_mode: str
    site_ids: frozenset[UUID]
    jti: str
    session_id: str
```
(added `site_scope_mode`/`site_ids`, both required — every caller of
`AccessTokenClaims` is updated below, there's no partial-construction call site left
after this task).

```python
def issue_access_token(
    *,
    settings: Settings,
    user_id: UUID,
    audience: str,
    tenant_id: UUID | None,
    membership_id: UUID | None,
    permissions: frozenset[str],
    session_id: str,
    site_scope_mode: str = "none",
    site_ids: frozenset[UUID] = frozenset(),
    key_id: str = "local-dev-1",
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": audience,
        "sub": str(user_id),
        "iat": now,
        "nbf": now,
        "exp": now + settings.jwt_access_token_ttl_seconds,
        "jti": str(uuid4()),
        "sid": session_id,
        "perm": sorted(permissions),
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    if membership_id is not None:
        payload["membership_id"] = str(membership_id)
    if tenant_id is not None:
        # Only meaningful alongside a real tenant membership - a platform-audience token
        # (no tenant_id) never carries these at all, matching how tenant_id/membership_id
        # are already conditionally included above.
        payload["ssm"] = site_scope_mode
        if site_ids:
            payload["sids"] = sorted(str(s) for s in site_ids)

    return jwt.encode(
        payload,
        settings.jwt_private_key,
        algorithm=settings.jwt_algorithm,
        headers={"kid": key_id},
    )
```
(`site_scope_mode`/`site_ids` default to the safe "sees nothing extra" shape so every
existing call site that doesn't pass them — none should remain after this task, but the
defaults keep the signature backward-compatible during the edit — still produces a valid
token).

```python
def decode_access_token(
    token: str, *, settings: Settings, expected_audience: str
) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
            audience=expected_audience,
            issuer=settings.jwt_issuer,
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc

    tenant_id = payload.get("tenant_id")
    membership_id = payload.get("membership_id")
    return AccessTokenClaims(
        subject_user_id=UUID(payload["sub"]),
        audience=payload["aud"],
        tenant_id=UUID(tenant_id) if tenant_id else None,
        membership_id=UUID(membership_id) if membership_id else None,
        permissions=frozenset(payload.get("perm", [])),
        site_scope_mode=payload.get("ssm", "none"),
        site_ids=frozenset(UUID(s) for s in payload.get("sids", [])),
        jti=payload["jti"],
        session_id=payload["sid"],
    )
```

- [ ] **Step 2: Extend `ActiveMembership` and `get_first_active_membership`**

In `backend/tenant_api/app/repositories/identity.py`:

```python
@dataclass(frozen=True)
class ActiveMembership:
    membership_id: UUID
    tenant_id: UUID
    role_id: UUID
    site_scope_mode: str
    site_ids: frozenset[UUID]


async def get_first_active_membership(session: AsyncSession, user_id: UUID) -> ActiveMembership | None:
    """Resolves the authenticating user's own active membership before tenant scope
    exists. Backed by a SECURITY DEFINER function (migration 0005, extended by 0055 to
    also return site scope) restricted to a single user's own membership - not a general
    cross-tenant query."""
    result = await session.execute(
        text(
            "SELECT membership_id, tenant_id, role_id, site_scope_mode, site_ids "
            "FROM csense_active_membership_for_user(:user_id)"
        ),
        {"user_id": str(user_id)},
    )
    row = result.first()
    if row is None:
        return None
    return ActiveMembership(
        membership_id=row[0], tenant_id=row[1], role_id=row[2],
        site_scope_mode=row[3], site_ids=frozenset(row[4] or []),
    )
```

- [ ] **Step 3: Pass scope through every token-issuance call site in `auth.py`**

`register` (owner always gets `all` — an owner scoping themselves out of their own
tenant's sites at creation time is not a real scenario):
```python
    return await _issue_tokens(
        request, response, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, permissions=permissions,
        site_scope_mode="all", site_ids=frozenset(),
    )
```

`accept_invitation` (reads the real, just-activated membership's own scope):
```python
        permissions = await get_role_permissions(db, membership.role_id)
        site_scope_mode = membership.site_scope_mode
        site_ids = frozenset(
            row[0] for row in (
                await db.execute(
                    text(
                        "SELECT resource_id FROM membership_resource_scopes "
                        "WHERE membership_id = :mid AND resource_type = 'site' AND effect = 'allow'"
                    ),
                    {"mid": membership.id},
                )
            ).all()
        )
        user_id, tenant_id, membership_id = user.id, membership.tenant_id, membership.id

    return await _issue_tokens(
        request, response, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, permissions=permissions,
        site_scope_mode=site_scope_mode, site_ids=site_ids,
    )
```

`login` (uses the now-extended `ActiveMembership`):
```python
        membership = await get_first_active_membership(db, user.id)
        if membership is None:
            raise AuthenticationError("No active tenant membership for this account.")

        permissions = await get_role_permissions(db, membership.role_id)
        user_id, tenant_id, membership_id = user.id, membership.tenant_id, membership.membership_id

    return await _issue_tokens(
        request, response, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, permissions=permissions,
        site_scope_mode=membership.site_scope_mode, site_ids=membership.site_ids,
    )
```

`_issue_tokens` itself gains the two parameters and forwards them:
```python
async def _issue_tokens(
    request: Request,
    response: Response,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    membership_id: uuid.UUID,
    permissions: frozenset[str],
    site_scope_mode: str,
    site_ids: frozenset[uuid.UUID],
) -> AuthResponse:
    redis_client = request.app.state.redis
    session_id, refresh_token = await create_session(
        redis_client, settings,
        user_id=user_id, tenant_id=tenant_id, membership_id=membership_id, audience=AUDIENCE_CUSTOMER,
    )
    access_token = issue_access_token(
        settings=settings,
        user_id=user_id,
        audience=AUDIENCE_CUSTOMER,
        tenant_id=tenant_id,
        membership_id=membership_id,
        permissions=permissions,
        session_id=session_id,
        site_scope_mode=site_scope_mode,
        site_ids=site_ids,
    )
    _set_refresh_cookies(response, session_id=session_id, refresh_token=refresh_token)
    return AuthResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_ttl_seconds,
        tenant_id=str(tenant_id),
    )
```

`refresh` (same `ActiveMembership` shape as `login`):
```python
    session_factory: async_sessionmaker = request.app.state.session_factory
    async with bootstrap_session(session_factory) as db:
        membership = await get_first_active_membership(db, uuid.UUID(record["user_id"]))
        if membership is None:
            raise AuthenticationError("No active tenant membership for this account.")
        permissions = await get_role_permissions(db, membership.role_id)

    access_token = issue_access_token(
        settings=settings,
        user_id=uuid.UUID(record["user_id"]),
        audience=AUDIENCE_CUSTOMER,
        tenant_id=membership.tenant_id,
        membership_id=membership.membership_id,
        permissions=permissions,
        session_id=session_id,
        site_scope_mode=membership.site_scope_mode,
        site_ids=membership.site_ids,
    )
```

- [ ] **Step 4: Populate `TenantContext` from the decoded claims in `deps.py`**

In `backend/tenant_api/app/deps.py`, `current_tenant_context`:
```python
    return TenantContext(
        tenant_id=claims.tenant_id,
        user_id=claims.subject_user_id,
        membership_id=claims.membership_id,
        token_audience=claims.audience,
        permissions=claims.permissions,
        site_scope_mode=claims.site_scope_mode,
        site_ids=claims.site_ids,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
```
The support-grant-elevated path (`_elevated_context_from_platform_token`) is left
unchanged — it has no real membership row to read scope from, and `TenantContext`'s own
default (`site_scope_mode="none"` — wait, that would lock an elevated support session
out of every site, which is wrong for a legitimate support action). Pass `"all"`
explicitly there instead, since an approved support grant is a deliberate, narrow
elevation already gated by its own approved-scopes mechanism (`elevated.permissions`),
not a real per-site-scoped membership:
```python
    return TenantContext(
        tenant_id=tenant_id,
        user_id=claims.subject_user_id,
        membership_id=None,
        token_audience=claims.audience,
        permissions=elevated.permissions,
        site_scope_mode="all",
        support_grant_id=elevated.grant_id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
```

- [ ] **Step 5: Run the full backend test suite — this touches shared token code, every existing test must still pass**

```bash
cd backend && python3 -m pytest tests/ -q
```
Expected: same pass count as before this task, plus the 9 new `test_site_scope.py`
tests; no new failures. (If any test constructs `AccessTokenClaims` directly with
positional args, it will now fail on the two new required fields — fix by adding
`site_scope_mode="all", site_ids=frozenset()` at each such call site; grep
`AccessTokenClaims(` across `backend/tests/` to find them before running.)

- [ ] **Step 6: Commit**

```bash
git add backend/shared/csense_shared/security/tokens.py \
        backend/tenant_api/app/repositories/identity.py \
        backend/tenant_api/app/api/auth.py \
        backend/tenant_api/app/deps.py
git commit -m "auth: carry site_scope_mode/site_ids as real JWT claims (ssm/sids)"
```

### Task 4: Accept `selected` + `site_ids` in the memberships API

**Files:**
- Modify: `backend/tenant_api/app/api/memberships.py`

- [ ] **Step 1: Loosen the scope-mode validators and add `site_ids`**

```python
class InviteIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    role_name: str = Field(pattern="^(tenant_owner|tenant_operator|tenant_member|tenant_viewer)$")
    site_scope_mode: str = Field(default="none", pattern="^(all|selected|none)$")
    site_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("site_ids")
    @classmethod
    def _selected_needs_ids_elsewhere(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        # Real cross-field validation (site_scope_mode == "selected" implies len > 0)
        # happens in invite_member itself, not here - Pydantic v2 field_validators don't
        # see sibling fields without a model_validator, and the real error needs to name
        # which sites don't belong to this tenant anyway (a DB check), which can't happen
        # in a pure field validator.
        return value
```
(add `field_validator` to the existing import line: `from pydantic import BaseModel,
EmailStr, Field, field_validator`.)

```python
class MembershipPatchIn(BaseModel):
    role_name: str | None = Field(
        default=None, pattern="^(tenant_owner|tenant_operator|tenant_member|tenant_viewer)$"
    )
    status: str | None = Field(default=None, pattern="^(active|suspended|revoked)$")
    site_scope_mode: str | None = Field(default=None, pattern="^(all|selected|none)$")
    site_ids: list[uuid.UUID] | None = None
```

- [ ] **Step 2: Write a real DB-backed helper that validates + writes the scope rows**

Add to `backend/tenant_api/app/api/memberships.py`, above `invite_member`:
```python
async def _replace_site_scope(
    db: AsyncSession, *, tenant_id: uuid.UUID, membership_id: uuid.UUID,
    site_scope_mode: str, site_ids: list[uuid.UUID],
) -> None:
    if site_scope_mode == "selected" and not site_ids:
        raise ApiError(
            status_code=422, code="selected_scope_needs_sites",
            message="Pick at least one site, or choose a different site-access option.",
        )
    if site_ids:
        found = (
            await db.execute(
                text("SELECT count(*) FROM sites WHERE id = ANY(:ids) AND deleted_at IS NULL"),
                {"ids": site_ids},
            )
        ).scalar_one()
        if found != len(set(site_ids)):
            raise ApiError(
                status_code=422, code="unknown_site",
                message="One or more selected sites don't exist in this tenant.",
            )

    # Full replace, not a diff - the picker UI always submits the complete desired set,
    # same as how role_name/status are always full replacements in this same endpoint.
    await db.execute(
        text(
            "DELETE FROM membership_resource_scopes "
            "WHERE membership_id = :mid AND resource_type = 'site'"
        ),
        {"mid": membership_id},
    )
    for site_id in set(site_ids):
        await db.execute(
            text(
                "INSERT INTO membership_resource_scopes "
                "(tenant_id, membership_id, resource_type, resource_id, effect) "
                "VALUES (:tid, :mid, 'site', :sid, 'allow')"
            ),
            {"tid": tenant_id, "mid": membership_id, "sid": site_id},
        )
```

- [ ] **Step 3: Call it from `invite_member`**

After the existing `user, membership = await create_invited_membership(...)` call in
`invite_member`, before the `record_audit_and_outbox` call:
```python
    user, membership = await create_invited_membership(
        db, tenant_id=context.tenant_id, email=body.email, display_name=body.display_name,
        role=role, site_scope_mode=body.site_scope_mode, invited_by=context.user_id,
    )

    if body.site_scope_mode == "selected":
        await _replace_site_scope(
            db, tenant_id=context.tenant_id, membership_id=membership.id,
            site_scope_mode=body.site_scope_mode, site_ids=body.site_ids,
        )
```

- [ ] **Step 4: Call it from `update_membership`**

After the existing `if body.site_scope_mode is not None: membership.site_scope_mode =
body.site_scope_mode` line in `update_membership`, before `await db.flush()`:
```python
    if body.site_scope_mode is not None:
        membership.site_scope_mode = body.site_scope_mode
        await _replace_site_scope(
            db, tenant_id=context.tenant_id, membership_id=membership.id,
            site_scope_mode=body.site_scope_mode, site_ids=body.site_ids or [],
        )
    await db.flush()
```

- [ ] **Step 5: Restart tenant-api and confirm the module still imports cleanly**

```bash
cd infra && docker compose --env-file ../.env restart tenant-api
docker compose --env-file ../.env logs tenant-api --tail 30
```
Expected: no import/startup errors in the log tail.

- [ ] **Step 6: Commit**

```bash
git add backend/tenant_api/app/api/memberships.py
git commit -m "memberships: accept selected/site_ids, write membership_resource_scopes for real"
```

### Task 5: Enforce the filter on the five site-scoped list endpoints

**Files:**
- Modify: `backend/tenant_api/app/api/sites.py`
- Modify: `backend/tenant_api/app/api/cameras.py`
- Modify: `backend/tenant_api/app/api/zones.py`
- Modify: `backend/tenant_api/app/api/incidents.py`
- Modify: `backend/tenant_api/app/api/rules.py`

- [ ] **Step 1: `sites.py` — `list_sites` and `get_site`**

```python
@router.get("", response_model=list[SiteOut])
async def list_sites(
    limit: int = Query(default=100, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[SiteOut]:
    require_permission(context, "site.read")
    scope_clause, scope_params = site_scope_sql_filter(context, column="s.id")
    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE s.deleted_at IS NULL AND {scope_clause} ORDER BY s.name LIMIT :limit"),
            {"limit": limit, **scope_params},
        )
    ).all()
    return [_to_site(row) for row in rows]


@router.get("/{site_id}", response_model=SiteOut)
async def get_site(
    site_id: uuid.UUID,
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> SiteOut:
    require_permission(context, "site.read")
    if not context.can_access_site(site_id):
        # Same "not found covers both missing and out-of-scope" shape load_camera's own
        # docstring already establishes for cross-tenant cameras - a scoped-out site
        # should look identical to a nonexistent one, not reveal that it exists but is
        # off-limits.
        raise NotFoundError("No such site.")
    return await _load(db, site_id)
```
Add the import: `from csense_shared.security.site_scope import site_scope_sql_filter`.

- [ ] **Step 2: `cameras.py` — `list_cameras` and `load_camera`**

```python
@router.get("", response_model=list[CameraOut])
async def list_cameras(
    site_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(provisioning|ready|disabled)$"),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[CameraOut]:
    require_permission(context, "camera.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="c.site_id")
    clauses = ["c.deleted_at IS NULL", scope_clause]
    params: dict = {"limit": limit, **scope_params}
    if site_id:
        clauses.append("c.site_id = :site_id")
        params["site_id"] = site_id
    if status:
        clauses.append("c.status = CAST(:status AS camera_status)")
        params["status"] = status

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY c.name LIMIT :limit"),
            params,
        )
    ).all()
    return [_to_camera(row) for row in rows]
```
(only the body changed — the `site_id` query param already lets a caller ask for one
site explicitly; the scope clause now additionally restricts *which* sites they're ever
allowed to ask for, the same way `require_permission` already restricts which actions.)

`load_camera` is reused by `media.py`'s live-session endpoints (per its own docstring),
so it needs a `context` parameter added — check every call site when making this change:
```python
async def load_camera(db: AsyncSession, camera_id: uuid.UUID, context: TenantContext) -> CameraOut:
    """Public (not `_load`) - `media.py`'s live-session endpoints reuse this rather than
    re-deriving the same RLS-scoped "not found covers both missing and another tenant's
    camera" lookup. Now also covers "not found covers a real camera this membership is
    scoped out of" the same way."""
    row = (
        await db.execute(
            text(_SELECT + " WHERE c.id = :id AND c.deleted_at IS NULL"),
            {"id": camera_id},
        )
    ).first()
    if row is None:
        raise NotFoundError("No such camera.")
    camera = _to_camera(row)
    if not context.can_access_site(camera.site_id):
        raise NotFoundError("No such camera.")
    return camera
```
Every existing call site of `load_camera(db, camera_id)` becomes
`load_camera(db, camera_id, context)`. Confirmed by a repo-wide grep, these are the
exact 10 call sites to update (every one of them is inside a handler that already has
`context: TenantContext = Depends(current_tenant_context)` in its own signature, so
`context` is always already in scope at the call site — no handler signature needs to
change, only the call itself):

- `backend/tenant_api/app/api/media.py:108` — `camera = await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:211` — `return await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:292` — `return await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:303` — `await load_camera(db, camera_id)  # 404s before doing anything`
- `backend/tenant_api/app/api/cameras.py:307` — `return await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:324` — `return await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:380` — `camera = await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:414` — `return await load_camera(db, camera_id)`
- `backend/tenant_api/app/api/cameras.py:472` — `camera = await load_camera(db, camera_id)`

Each becomes the same call with `, context` appended before the closing paren — e.g.
`camera = await load_camera(db, camera_id, context)`. Re-run `grep -n "load_camera(" backend/tenant_api/app/api/*.py` after editing: it should
show exactly 10 lines total — the one 3-parameter definition (`cameras.py:161`) plus
the 9 call sites listed above, every one of them now passing `context` as its third
argument. Fewer than 9 updated call sites means one was missed.

Add the import to `cameras.py`: `from csense_shared.security.site_scope import
site_scope_sql_filter`.

- [ ] **Step 3: `zones.py` — `list_zones`**

```python
@router.get("", response_model=list[ZoneOut])
async def list_zones(
    site_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[ZoneOut]:
    require_permission(context, "zone.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="z.site_id")
    clauses = ["z.status <> 'deleted'", scope_clause]
    params: dict = {"limit": limit, **scope_params}
    if site_id:
        clauses.append("z.site_id = :site_id")
        params["site_id"] = site_id

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY z.name LIMIT :limit"),
            params,
        )
    ).all()
    return [_to_zone(row) for row in rows]
```
(only `clauses`/`params`' seeding changed — `require_permission`, the two optional
filters, and the final query/return are unchanged from the existing function). Add the
import: `from csense_shared.security.site_scope import site_scope_sql_filter`.

- [ ] **Step 4: `incidents.py` — `list_incidents`**

```python
    require_permission(context, "incident.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="site_id")
    filters = [scope_clause]
    params: dict = {"limit": limit + 1, **scope_params}
```
(the rest of `list_incidents`'s existing filter-building — `status`, `severity`,
`camera_id`, `cursor` — is unchanged; this just seeds `filters`/`params` with the scope
clause instead of starting from an empty list, since `incidents.site_id` has no table
alias in the existing query). Add the same import.

- [ ] **Step 5: `rules.py` — `list_rules`**

```python
@router.get("", response_model=list[RuleOut])
async def list_rules(
    site_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
    limit: int = Query(default=100, ge=1, le=200),
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> list[RuleOut]:
    require_permission(context, "rule.read")

    scope_clause, scope_params = site_scope_sql_filter(context, column="r.site_id")
    clauses = [scope_clause]
    params: dict = {"limit": limit, **scope_params}
    if site_id:
        clauses.append("r.site_id = :site_id")
        params["site_id"] = site_id
    if camera_id:
        # Site-wide rules apply to this camera too, so they belong in the answer.
        clauses.append("(r.camera_id = :camera_id OR r.camera_id IS NULL)")
        params["camera_id"] = camera_id
    if status:
        clauses.append("r.status = :status")
        params["status"] = status

    rows = (
        await db.execute(
            text(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY r.name LIMIT :limit"),
            params,
        )
    ).all()
    return [_to_rule(row) for row in rows]
```
(`clauses` was seeded with the literal `"TRUE"` before this change — now the scope
clause itself, which is `"TRUE"` for an `all`-scoped caller anyway, so an unrestricted
caller's query is byte-for-byte unaffected; everything else in the function is
unchanged). Add the import: `from csense_shared.security.site_scope import
site_scope_sql_filter`.

- [ ] **Step 6: Restart tenant-api and confirm it still imports cleanly**

```bash
cd infra && docker compose --env-file ../.env restart tenant-api
docker compose --env-file ../.env logs tenant-api --tail 40
```
Expected: no import errors. A missed `load_camera` call site (Step 2's known risk) would
show here as an `ImportError`/`AttributeError` only if it's a module-level reference;
a signature mismatch instead surfaces at request time — covered by Task 6's e2e run.

- [ ] **Step 7: Commit**

```bash
git add backend/tenant_api/app/api/sites.py backend/tenant_api/app/api/cameras.py \
        backend/tenant_api/app/api/zones.py backend/tenant_api/app/api/incidents.py \
        backend/tenant_api/app/api/rules.py
git commit -m "site-scope: enforce membership site scoping on sites/cameras/zones/incidents/rules lists"
```

**Named, deliberate scope boundary** (matching this project's own `[~]` convention):
per-resource `GET`/`PATCH` enforcement (a `selected`-scoped member fetching a single
zone/rule/incident by ID they aren't scoped to) is only wired for `sites.get_site` and
`cameras.load_camera` in this pass — the two most security-relevant single-resource
reads (a site's own detail, and the camera identity that gates live-view access).
Zone/rule/incident single-item `GET` endpoints still only enforce tenant boundary (RLS)
today, not site-membership scope; a `selected`-scoped member could still fetch a single
zone/rule/incident by ID outside their assigned sites if they already know or guess its
UUID. This is a real, named gap for a follow-up pass, not a silently missed case.

### Task 6: Frontend — the site multi-select picker

**Files:**
- Modify: `frontend/customer-crm/src/api/memberships.ts`
- Modify: `frontend/customer-crm/src/pages/TeamPage.tsx`

- [ ] **Step 1: Extend the TypeScript types**

```typescript
export interface Membership {
  id: string;
  user_id: string;
  email: string;
  display_name: string;
  role_name: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
  status: "invited" | "active" | "suspended" | "revoked";
  site_scope_mode: "all" | "selected" | "none";
  site_ids: string[];
  invited_at: string | null;
  accepted_at: string | null;
}

export interface InviteInput {
  email: string;
  display_name: string;
  role_name: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
  site_scope_mode?: "all" | "selected" | "none";
  site_ids?: string[];
}

export interface MembershipPatch {
  role_name?: "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer";
  status?: "active" | "suspended" | "revoked";
  site_scope_mode?: "all" | "selected" | "none";
  site_ids?: string[];
}
```
(the backend's `MembershipOut` response model needs `site_ids: list[str]` added too —
`backend/tenant_api/app/api/memberships.py`'s `MembershipOut`/`_SELECT`/`_to_out` need a
join to `membership_resource_scopes` to populate it; add
`array_remove(array_agg(mrs.resource_id), NULL) AS site_ids` via a `LEFT JOIN
membership_resource_scopes mrs ON mrs.membership_id = m.id AND mrs.resource_type =
'site' AND mrs.effect = 'allow'` plus a `GROUP BY` on every other selected column, and
extend `MembershipOut`/`_to_out` to carry it — this is the same real data
`_replace_site_scope` already writes, just not yet read back by the list endpoint; do
this as part of this task's Step 1 in the backend file too, it was not listed
separately in File Structure because it's a small addition to a file Task 4 already
modifies).

- [ ] **Step 2: Build the site picker using the codebase's own existing chip-row pattern**

In `frontend/customer-crm/src/pages/TeamPage.tsx`, `InviteDialog` needs the tenant's
site list (reuse `listSites` from `../api/sites`, the same function `RulesPage.tsx`
already uses for its own site dropdown):

```typescript
import { listSites, type Site } from "../api/sites";
```

```typescript
function InviteDialog({
  onClose,
  onInvited,
}: {
  onClose: () => void;
  onInvited: (member: import("../api/memberships").InviteResult) => void;
}) {
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [roleName, setRoleName] = useState<
    "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer"
  >("tenant_member");
  const [siteScopeMode, setSiteScopeMode] = useState<"all" | "selected" | "none">("none");
  const [selectedSiteIds, setSelectedSiteIds] = useState<string[]>([]);
  const [sites, setSites] = useState<Site[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listSites().then(setSites).catch(() => setSites([]));
  }, []);

  function toggleSite(id: string) {
    setSelectedSiteIds((current) =>
      current.includes(id) ? current.filter((s) => s !== id) : [...current, id],
    );
  }

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      const member = await inviteMember({
        email, display_name: displayName, role_name: roleName,
        site_scope_mode: siteScopeMode,
        site_ids: siteScopeMode === "selected" ? selectedSiteIds : undefined,
      });
      onInvited(member);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.body.message : "Could not send this invitation.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open title="Invite a team member" onClose={onClose}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label>
          Name
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </label>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Role
          <select
            value={roleName}
            onChange={(e) =>
              setRoleName(
                e.target.value as "tenant_owner" | "tenant_operator" | "tenant_member" | "tenant_viewer",
              )
            }
          >
            <option value="tenant_viewer">Viewer (read-only)</option>
            <option value="tenant_member">Member</option>
            <option value="tenant_operator">Operator (cameras, rules, incidents)</option>
            <option value="tenant_owner">Owner</option>
          </select>
        </label>
        <label>
          Site access
          <select
            value={siteScopeMode}
            onChange={(e) => setSiteScopeMode(e.target.value as "all" | "selected" | "none")}
          >
            <option value="none">No sites (assign later)</option>
            <option value="selected">Selected sites only</option>
            <option value="all">All sites</option>
          </select>
        </label>
        {siteScopeMode === "selected" && (
          <div className="chip-row">
            {sites.map((site) => (
              <label key={site.id} className="chip">
                <input
                  type="checkbox"
                  checked={selectedSiteIds.includes(site.id)}
                  onChange={() => toggleSite(site.id)}
                />
                {site.name}
              </label>
            ))}
            {sites.length === 0 && <small className="muted">No sites exist yet.</small>}
          </div>
        )}

        {submitting && <InlineSpinner label="Sending invitation…" />}
        {!submitting && error && (
          <p role="alert" className="error-panel">
            {error}
          </p>
        )}

        <div className="form-actions">
          <button
            type="button"
            className="primary"
            disabled={
              !email || !displayName || submitting ||
              (siteScopeMode === "selected" && selectedSiteIds.length === 0)
            }
            onClick={() => void handleSubmit()}
          >
            Send invitation
          </button>
        </div>
      </div>
    </Dialog>
  );
}
```
(the disabled-button guard for "selected with zero sites checked" mirrors the backend's
own `422 selected_scope_needs_sites` refusal from Task 4 — caught in the UI before the
round trip, not only after).

- [ ] **Step 3: Confirm the build compiles**

```bash
cd frontend/customer-crm && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add frontend/customer-crm/src/api/memberships.ts frontend/customer-crm/src/pages/TeamPage.tsx \
        backend/tenant_api/app/api/memberships.py
git commit -m "customer-crm: real per-site picker UI for member invites (chip-row, matches RulesPage)"
```

### Task 7: Verify for real — e2e script

**Files:**
- Create: `scripts/e2e_site_scoping.py`

- [ ] **Step 1: Write the script**

```python
"""End-to-end verification of per-site membership scoping (migrations 0055, the JWT
ssm/sids claims, and the five enforced list endpoints). Proves against the real running
stack:

  1. Register a tenant, create two real sites (A, B), one camera on each.
  2. Invite a real member scoped to site A only ("selected"), accept the invitation for
     real, log in for real.
  3. Confirm that member's real JWT carries ssm="selected" and sids=[site_a_id] - not by
     inspection of the DB, by decoding the actual token this login call returned.
  4. Confirm the real, enforced HTTP behaviour: GET /api/v1/tenant/sites returns only
     site A; GET /api/v1/tenant/cameras returns only site A's camera; GET
     /api/v1/tenant/sites/{site_b_id} 404s (not 403 - scoped-out looks like nonexistent).
  5. Confirm the owner (site_scope_mode="all") still sees both sites and both cameras -
     regression check, this feature must not narrow the owner's own view.
  6. Confirm a 'none'-scoped invite (the new real default) sees zero sites and zero
     cameras.

Run from the repo root with the stack up:
    python scripts/e2e_site_scoping.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:8080"
PASSWORD = "E2ESiteScoping!Password123"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def redis_get_invitation_token(email: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "--scan", "--pattern", "invite:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    for key in result.stdout.strip().splitlines():
        value = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis", "redis-cli", "get", key],
            cwd="infra", capture_output=True, text=True, check=True,
        ).stdout.strip()
        if email in value:
            return key.split("invite:", 1)[1]
    raise RuntimeError(f"No invitation token found in Redis for {email}")


def decode_jwt_claims(token: str) -> dict:
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Register a tenant, create sites A and B, one camera on each")
    owner_email = f"scope-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Site Scoping E2E {suffix}",
        "email": owner_email, "password": PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token = auth["access_token"]

    _, site_a = api("/api/v1/tenant/sites", {"name": "Site A", "code": f"a-{suffix}"}, owner_token, expect=(201,))
    _, site_b = api("/api/v1/tenant/sites", {"name": "Site B", "code": f"b-{suffix}"}, owner_token, expect=(201,))
    site_a_id, site_b_id = site_a["id"], site_b["id"]

    _, camera_a = api("/api/v1/tenant/cameras", {
        "site_id": site_a_id, "name": "Cam A", "code": f"cam-a-{suffix}",
    }, owner_token, expect=(201,))
    api("/api/v1/tenant/cameras", {
        "site_id": site_b_id, "name": "Cam B", "code": f"cam-b-{suffix}",
    }, owner_token, expect=(201,))

    step(2, "Invite a member scoped to Site A only, accept and log in for real")
    scoped_email = f"scoped-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": scoped_email, "display_name": "Scoped", "role_name": "tenant_member",
        "site_scope_mode": "selected", "site_ids": [site_a_id],
    }, owner_token, expect=(201,))
    ticket = redis_get_invitation_token(scoped_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket, "password": PASSWORD}, expect=(200,))
    _, scoped_auth = api("/api/v1/auth/login", {"email": scoped_email, "password": PASSWORD})
    scoped_token = scoped_auth["access_token"]

    step(3, "Confirm the real JWT carries ssm=selected and sids=[site_a_id]")
    claims = decode_jwt_claims(scoped_token)
    check(claims.get("ssm") == "selected", f"real token has ssm=selected (got {claims.get('ssm')})", failures)
    check(claims.get("sids") == [site_a_id], f"real token has sids=[site A] (got {claims.get('sids')})", failures)

    step(4, "Confirm real, enforced HTTP behaviour for the scoped member")
    _, sites_seen = api("/api/v1/tenant/sites", token=scoped_token, method="GET", expect=(200,))
    check([s["id"] for s in sites_seen] == [site_a_id], "scoped member's site list contains only Site A", failures)

    _, cameras_seen = api("/api/v1/tenant/cameras", token=scoped_token, method="GET", expect=(200,))
    check(
        [c["id"] for c in cameras_seen] == [camera_a["id"]],
        "scoped member's camera list contains only Site A's camera", failures,
    )

    status, _ = api(f"/api/v1/tenant/sites/{site_b_id}", token=scoped_token, method="GET", expect=(200, 404))
    check(status == 404, f"scoped member's GET on Site B real-404s, not 403 (got {status})", failures)

    step(5, "Confirm the owner still sees both sites and both cameras (no regression)")
    _, owner_sites = api("/api/v1/tenant/sites", token=owner_token, method="GET", expect=(200,))
    check(len(owner_sites) == 2, f"owner still sees both sites (got {len(owner_sites)})", failures)
    _, owner_cameras = api("/api/v1/tenant/cameras", token=owner_token, method="GET", expect=(200,))
    check(len(owner_cameras) == 2, f"owner still sees both cameras (got {len(owner_cameras)})", failures)

    step(6, "Confirm a 'none'-scoped invite (the new real default) sees zero sites")
    none_email = f"none-{suffix}@example.com"
    api("/api/v1/tenant/memberships", {
        "email": none_email, "display_name": "NoAccess", "role_name": "tenant_member",
    }, owner_token, expect=(201,))
    none_ticket = redis_get_invitation_token(none_email)
    api("/api/v1/auth/accept-invitation", {"token": none_ticket, "password": PASSWORD}, expect=(200,))
    _, none_auth = api("/api/v1/auth/login", {"email": none_email, "password": PASSWORD})
    _, none_sites = api("/api/v1/tenant/sites", token=none_auth["access_token"], method="GET", expect=(200,))
    check(none_sites == [], f"a fresh 'none'-scoped invite (the real default) sees zero sites (got {len(none_sites)})", failures)

    step(7, "Clean up")
    tenant_id = auth["tenant_id"]
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    for email in (owner_email, scoped_email, none_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - site-scoping verified for real: selected/all/none each behave exactly as the JWT claims and HTTP responses show.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against the live stack**

```bash
python scripts/e2e_site_scoping.py
```
Expected: `PASS`.

- [ ] **Step 3: Run the full backend pytest suite as a final regression check**

```bash
cd backend && python3 -m pytest tests/ -q
```
Expected: no new failures versus the pre-Feature-A baseline.

- [ ] **Step 4: Commit**

```bash
git add scripts/e2e_site_scoping.py
git commit -m "e2e: verify per-site membership scoping against the real stack"
```

---

# Feature C: Admin-initiated license plan change

**The real gap, precisely:** `issue_license` refuses when a tenant already has an
active/grace license (its own code comment already says so: "Upgrade/downgrade/replace
is a real, separate flow... not built this pass"). `renew_license` only extends a term —
it never touches `plan_id`. Nothing else writes `licenses.plan_id`. This task is that
missing flow.

**Design decision:** change-plan supersedes — the existing license row is marked
`revoked` (a real terminal status this schema already has; no new enum value needed) and
a brand-new license row is created under the new plan, reusing the exact same
entitlement/quota-ledger provisioning `issue_license` already does (extracted into a
shared helper so the logic isn't duplicated). **Quota usage does not carry over** —
`quota_ledgers.consumed_value` starts fresh under the new license, the same as any new
license already does; this is a deliberate choice (avoiding a tenant inheriting usage
counted against a different plan's terms), stated here the same way `renew_license`'s
own docstring states its choices, not left implicit.

### Task 1: Extract shared entitlement-provisioning logic

**Files:**
- Modify: `backend/admin_api/app/api/licensing.py`

- [ ] **Step 1: Extract `_provision_entitlements` out of `issue_license`'s body**

Add this function above `issue_license` (after `_load_entitlements`):
```python
async def _provision_entitlements(
    db: AsyncSession, *, tenant_id: uuid.UUID, license_id: uuid.UUID,
    merged: dict[str, EntitlementSpec], starts_at: dt.datetime, expires_at: dt.datetime | None,
) -> None:
    """Writes license_entitlements + quota_ledgers rows for a license - shared by
    issue_license (a brand-new license) and change_plan (a license superseding an old
    one). Always starts quota_ledgers fresh (consumed_value defaults to 0 via the
    column's own server default) - a deliberate choice, not an oversight: usage counted
    against a different plan's terms should not silently carry over to a new one."""
    for code, spec in merged.items():
        await db.execute(
            text(
                "INSERT INTO license_entitlements "
                "(tenant_id, license_id, entitlement_code, value_type, limit_numeric, enabled_boolean, value_json) "
                "VALUES (:tenant_id, :license_id, :code, :value_type, :limit_numeric, :enabled_boolean, "
                "CAST(:value_json AS jsonb))"
            ),
            {
                "tenant_id": tenant_id, "license_id": license_id, "code": code,
                "value_type": spec.value_type, "limit_numeric": spec.limit_numeric,
                "enabled_boolean": spec.enabled_boolean,
                "value_json": json.dumps(spec.value_json) if spec.value_json is not None else None,
            },
        )
        if spec.value_type == "limit_numeric" and spec.limit_numeric is not None:
            await db.execute(
                text(
                    "INSERT INTO quota_ledgers "
                    "(tenant_id, license_id, quota_code, period_start, period_end, limit_value) "
                    "VALUES (:tenant_id, :license_id, :code, :period_start, :period_end, :limit_value)"
                ),
                {
                    "tenant_id": tenant_id, "license_id": license_id, "code": code,
                    "period_start": starts_at, "period_end": expires_at, "limit_value": spec.limit_numeric,
                },
            )
```

- [ ] **Step 2: Replace `issue_license`'s own inline loop with a call to it**

In `issue_license`, replace the existing `for code, spec in merged.items(): ...` block
(the loop that inserts `license_entitlements`/`quota_ledgers`) with:
```python
    await _provision_entitlements(
        db, tenant_id=body.tenant_id, license_id=license_id,
        merged=merged, starts_at=starts_at, expires_at=body.expires_at,
    )
```

- [ ] **Step 3: Restart admin-api and re-run the existing licensing e2e as a regression check**

```bash
cd infra && docker compose --env-file ../.env restart admin-api
cd .. && python3 scripts/e2e_licensing.py
```
Expected: `PASS` — this confirms the extraction didn't change `issue_license`'s real
behavior at all (the e2e script already exercises real quota enforcement end to end).

- [ ] **Step 4: Commit**

```bash
git add backend/admin_api/app/api/licensing.py
git commit -m "licensing: extract _provision_entitlements, shared by issue and (upcoming) change-plan"
```

### Task 2: The `change-plan` endpoint

**Files:**
- Modify: `backend/admin_api/app/api/licensing.py`

- [ ] **Step 1: Add the request/response models and endpoint**

```python
class ChangeLicensePlanIn(BaseModel):
    new_plan_code: str
    expires_at: dt.datetime | None = None
    grace_days: int = Field(default=DEFAULT_GRACE_DAYS, ge=0, le=365)
    entitlement_overrides: dict[str, EntitlementSpec] = Field(default_factory=dict)


@router.post("/licenses/{license_id}/change-plan", response_model=LicenseOut)
async def change_license_plan(
    license_id: uuid.UUID,
    body: ChangeLicensePlanIn,
    request: Request,
    context: PlatformContext = Depends(current_platform_context),
    db: AsyncSession = Depends(platform_db_session),
    settings: Settings = Depends(get_app_settings),
) -> LicenseOut:
    """Moves a tenant from its current plan to a different one without hand-editing the
    database - the real gap issue_license's own refusal names ("Upgrade/downgrade/
    replace is a real, separate flow... not built this pass"). Supersedes rather than
    mutates in place: the existing license row is marked revoked (a real terminal status
    this schema already has - see the licenses table's own status enum) and a brand-new
    license row is created under the new plan, provisioned exactly like a fresh
    issue_license call (_provision_entitlements). Quota usage does not carry over - see
    _provision_entitlements's own docstring for why that's a deliberate choice."""
    require_permission(context, "license.manage")

    if not await has_recent_step_up(
        request.app.state.redis, settings, scope=STEP_UP_SCOPE, principal_id=context.developer_user_id
    ):
        raise ApiError(
            status_code=403, code="step_up_required",
            message="Changing a license's plan requires a recent MFA verification. "
            "Call POST /api/v1/admin/auth/mfa/verify, then retry.",
        )

    old_row = (
        await db.execute(
            text(
                "SELECT l.tenant_id, l.organization_id, l.status::text, p.code "
                "FROM licenses l JOIN license_plans p ON p.id = l.plan_id "
                "WHERE l.id = :id FOR UPDATE"
            ),
            {"id": license_id},
        )
    ).first()
    if old_row is None:
        raise NotFoundError("No such license.")
    tenant_id, organization_id, old_status, old_plan_code = old_row
    if old_status == "revoked":
        raise ConflictError("A revoked license cannot be changed - issue a new one instead.")
    if old_plan_code == body.new_plan_code:
        raise ConflictError(f"This tenant is already on plan '{body.new_plan_code}'.")

    new_plan_row = (
        await db.execute(
            text("SELECT id, default_entitlements FROM license_plans WHERE code = :code AND status = 'active'"),
            {"code": body.new_plan_code},
        )
    ).first()
    if new_plan_row is None:
        raise NotFoundError(f"No active license plan with code '{body.new_plan_code}'.")
    new_plan_id, default_entitlements = new_plan_row

    await db.execute(
        text("UPDATE licenses SET status = 'revoked', version = version + 1, updated_at = now() WHERE id = :id"),
        {"id": license_id},
    )

    merged: dict[str, EntitlementSpec] = {
        code: EntitlementSpec(**spec) for code, spec in default_entitlements.items()
    }
    merged.update(body.entitlement_overrides)
    grace_ends_at = (
        body.expires_at + dt.timedelta(days=body.grace_days) if body.expires_at is not None else None
    )

    new_license_row = (
        await db.execute(
            text(
                "INSERT INTO licenses "
                "(tenant_id, organization_id, plan_id, parent_license_id, expires_at, grace_ends_at) "
                "VALUES (:tenant_id, :organization_id, :plan_id, :parent_license_id, :expires_at, :grace_ends_at) "
                "RETURNING id, starts_at"
            ),
            {
                "tenant_id": tenant_id, "organization_id": organization_id,
                "plan_id": new_plan_id, "parent_license_id": license_id,
                "expires_at": body.expires_at, "grace_ends_at": grace_ends_at,
            },
        )
    ).first()
    new_license_id, starts_at = new_license_row[0], new_license_row[1]

    await _provision_entitlements(
        db, tenant_id=tenant_id, license_id=new_license_id,
        merged=merged, starts_at=starts_at, expires_at=body.expires_at,
    )

    await record_audit_and_outbox(
        db,
        tenant_id=tenant_id,
        actor_type="platform_developer",
        actor_id=str(context.developer_user_id),
        action="license.change_plan",
        outcome="success",
        target_type="license",
        target_id=str(new_license_id),
        reason=f"Changed plan from '{old_plan_code}' to '{body.new_plan_code}'",
        before_patch={"license_id": str(license_id), "plan_code": old_plan_code},
        after_patch={"license_id": str(new_license_id), "plan_code": body.new_plan_code},
        correlation_id=uuid.UUID(context.correlation_id) if context.correlation_id else None,
        event_type="license.plan_changed.v1",
        event_payload={
            "old_license_id": str(license_id), "new_license_id": str(new_license_id),
            "tenant_id": str(tenant_id), "old_plan_code": old_plan_code, "new_plan_code": body.new_plan_code,
        },
        aggregate_type="license",
        aggregate_id=str(new_license_id),
    )

    entitlements = await _load_entitlements(db, license_id=new_license_id)
    return LicenseOut(
        id=str(new_license_id), tenant_id=str(tenant_id), plan_code=body.new_plan_code,
        status="active", starts_at=starts_at.isoformat(),
        expires_at=body.expires_at.isoformat() if body.expires_at else None,
        grace_ends_at=grace_ends_at.isoformat() if grace_ends_at else None,
        entitlements=entitlements,
    )
```

- [ ] **Step 2: Restart admin-api**

```bash
cd infra && docker compose --env-file ../.env restart admin-api
docker compose --env-file ../.env logs admin-api --tail 30
```
Expected: no import/startup errors.

- [ ] **Step 3: Commit**

```bash
git add backend/admin_api/app/api/licensing.py
git commit -m "licensing: add POST /licenses/{id}/change-plan (supersede, no more manual DB edits)"
```

### Task 3: Frontend — the change-plan dialog

**Files:**
- Modify: `frontend/developer-console/src/api/licensing.ts`
- Create: `frontend/developer-console/src/components/ChangeLicensePlanDialog.tsx`
- Modify: `frontend/developer-console/src/app/dashboard/page.tsx`

- [ ] **Step 1: Add the API client function**

```typescript
export interface ChangeLicensePlanInput {
  new_plan_code: string;
  expires_at?: string;
  grace_days?: number;
  entitlement_overrides?: Record<string, EntitlementSpec>;
}

/** Supersedes the tenant's current license with a new one under a different plan - the
 *  real "no more manual DB edits" path issueLicense's own refusal (an already-active
 *  tenant can't be re-issued) names as missing. Same step-up gate as issueLicense. */
export function changeLicensePlan(licenseId: string, body: ChangeLicensePlanInput) {
  return apiFetch<License>(`/api/v1/admin/licenses/${licenseId}/change-plan`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
```

- [ ] **Step 2: Write the dialog, modeled directly on `IssueLicenseDialog.tsx`**

```tsx
"use client";

import type { License, LicensePlan } from "../api/licensing";
import { changeLicensePlan } from "../api/licensing";
import { verifyMfa } from "../api/mfa";
import { ApiRequestError } from "../api/client";
import { Dialog } from "./Dialog";
import { ErrorSummary, Field, FormActions, required, onSubmitHandler, useForm } from "./Form";

/** Changes an existing license's plan - mirrors ChangeLicensePlanIn in
 *  backend/admin_api/app/api/licensing.py. Same step-up-then-submit shape as
 *  IssueLicenseDialog (TRD-SEC-010): the code is verified right before the change, in
 *  the same submit, rather than trusting the session's own bearer token alone.
 */
export function ChangeLicensePlanDialog({
  license,
  plans,
  onClose,
  onChanged,
}: {
  license: License;
  plans: LicensePlan[];
  onClose: () => void;
  onChanged: (license: License) => void;
}) {
  const otherPlans = plans.filter((p) => p.code !== license.plan_code);

  const form = useForm({
    new_plan_code: { initial: otherPlans[0]?.code ?? "", label: "New plan", validate: required("New plan") },
    code: { initial: "", label: "Verification code", validate: required("Verification code") },
  });

  const submit = onSubmitHandler(form.validateAll, form.setSubmitAttempted, async () => {
    form.setSubmitting(true);
    form.setFormError(undefined);
    try {
      await verifyMfa({ code: form.values.code });
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        form.setFormError(
          err.body.message.includes("not enrolled")
            ? "Two-factor authentication isn't enabled for your account yet - turn it on in Settings before changing a plan."
            : "That code is incorrect. Check your authenticator app and try again.",
        );
      } else {
        form.applyServerError(err);
      }
      form.setSubmitting(false);
      return;
    }

    try {
      const updated = await changeLicensePlan(license.id, { new_plan_code: form.values.new_plan_code });
      onChanged(updated);
      onClose();
    } catch (err) {
      form.applyServerError(err);
    } finally {
      form.setSubmitting(false);
    }
  });

  return (
    <Dialog open title={`Change plan (currently ${license.plan_code})`} onClose={onClose}>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={form.visibleErrors} formError={form.formError} />

        {otherPlans.length > 0 ? (
          <Field
            {...form.field("new_plan_code")}
            label="New plan"
            required
            options={otherPlans.map((p) => ({ value: p.code, label: `${p.name} (${p.code})` }))}
          />
        ) : (
          <p className="muted">No other active plan exists to switch to.</p>
        )}

        <Field
          {...form.field("code")}
          label="Verification code"
          required
          type="text"
          placeholder="6-digit code"
          hint="A recent MFA verification is required to change a plan (TRD-SEC-010)."
        />

        <FormActions
          submitting={form.submitting}
          submitLabel="Change plan"
          onCancel={onClose}
        />
      </form>
    </Dialog>
  );
}
```

- [ ] **Step 3: Wire the dialog into the licenses table**

In `frontend/developer-console/src/app/dashboard/page.tsx`, add state and a button next
to each license row (locate the existing `<td>{license.status}</td>`-style row rendering
around the licenses table, matching the file's own current structure at implementation
time):
```typescript
const [changingPlan, setChangingPlan] = useState<License | null>(null);

function handlePlanChanged(license: License) {
  licenses.mutate((current) => (current ?? []).map((l) => (l.id === license.id ? license : l)));
  notify.success("License plan changed");
}
```
```tsx
<td>
  <button type="button" onClick={() => setChangingPlan(license)}>
    Change plan
  </button>
</td>
```
(add a header cell for this column alongside the existing ones, and:)
```tsx
{changingPlan && (
  <ChangeLicensePlanDialog
    license={changingPlan}
    plans={plans.data ?? []}
    onClose={() => setChangingPlan(null)}
    onChanged={handlePlanChanged}
  />
)}
```
Add the import: `import { ChangeLicensePlanDialog } from
"@/components/ChangeLicensePlanDialog";`.

- [ ] **Step 4: Confirm the build compiles**

```bash
cd frontend/developer-console && npm run build
```

- [ ] **Step 5: Commit**

```bash
git add frontend/developer-console/src/api/licensing.ts \
        frontend/developer-console/src/components/ChangeLicensePlanDialog.tsx \
        frontend/developer-console/src/app/dashboard/page.tsx
git commit -m "developer-console: add Change plan dialog (real UI for the new change-plan endpoint)"
```

### Task 4: Verify for real — e2e script

**Files:**
- Create: `scripts/e2e_license_change_plan.py`

- [ ] **Step 1: Write the script**

```python
"""End-to-end verification of admin-initiated license plan change - the real gap
issue_license's own refusal names ("Upgrade/downgrade/replace is a real, separate
flow... not built this pass"). Proves against the real running stack:

  1. Bootstrap a real platform admin, create two real plans with different
     camera.count limits (small=2, large=10).
  2. Issue the small plan to a real tenant, confirm a 3rd camera is really refused
     (402 quota_exceeded) - the existing, unmodified quota-enforcement path.
  3. Call the real POST /licenses/{id}/change-plan to move to the large plan.
  4. Confirm the OLD license row is really revoked (not just superseded in appearance) -
     a direct DB check - and the NEW license row is really active under the new plan.
  5. Confirm the real, previously-refused 3rd camera can now be created - proving the
     new quota_ledgers row is real, not just a status-string change.
  6. Confirm no manual DB write was used anywhere above except the initial platform-admin
     bootstrap (the same one every other licensing e2e script already needs, e.g.
     scripts/e2e_licensing.py's own bootstrap_platform_admin) - the plan change itself is
     100% through the real HTTP API.

Run from the repo root with the stack up:
    python scripts/e2e_license_change_plan.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password
from csense_shared.security.totp import totp_now

API = "http://localhost:8080"
PLATFORM_ADMIN_PASSWORD = "ChangePlanE2E!Platform123"
OWNER_PASSWORD = "ChangePlanOwner!Password456"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204), host="app.localhost"):
    headers = {"Content-Type": "application/json", "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def bootstrap_platform_admin() -> tuple[str, str]:
    """Mirrors scripts/e2e_licensing.py's own bootstrap_platform_admin exactly."""
    settings = get_settings()
    suffix = uuid.uuid4().hex[:8]
    email = f"changeplan-e2e-{suffix}@platform.dev"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Change Plan E2E') RETURNING id",
            (email, email, hash_password(PLATFORM_ADMIN_PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_developers (user_id, status) VALUES (%s, 'active') RETURNING id",
            (user_id,),
        )
        developer_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'platform_admin' AND audience = 'platform'"
        )
        role_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO platform_role_assignments (platform_developer_id, role_id, status) "
            "VALUES (%s, %s, 'active')",
            (developer_id, role_id),
        )
        conn.commit()
    return email, str(user_id)


def mfa_step_up(admin_token: str) -> None:
    _, enrolled = api("/api/v1/admin/auth/mfa/totp/enroll", token=admin_token, host="console.localhost", expect=(201,))
    code = totp_now(enrolled["secret"])
    api("/api/v1/admin/auth/mfa/totp/confirm", {"code": code}, admin_token, host="console.localhost", expect=(200,))
    code = totp_now(enrolled["secret"])
    api("/api/v1/admin/auth/mfa/verify", {"code": code}, admin_token, host="console.localhost", expect=(200,))


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap a real platform admin, create small (limit=2) and large (limit=10) plans")
    admin_email, _ = bootstrap_platform_admin()
    _, admin_auth = api("/api/v1/admin/auth/login", {"email": admin_email, "password": PLATFORM_ADMIN_PASSWORD}, host="console.localhost")
    admin_token = admin_auth["access_token"]
    mfa_step_up(admin_token)

    api("/api/v1/admin/license-plans", {
        "code": f"small-{suffix}", "name": "Small", "license_type": "standard", "billing_period": "yearly",
        "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 2}},
    }, admin_token, host="console.localhost", expect=(201,))
    api("/api/v1/admin/license-plans", {
        "code": f"large-{suffix}", "name": "Large", "license_type": "standard", "billing_period": "yearly",
        "default_entitlements": {"camera.count": {"value_type": "limit_numeric", "limit_numeric": 10}},
    }, admin_token, host="console.localhost", expect=(201,))

    step(2, "Register a tenant, issue the small plan, confirm the real 402 on a 3rd camera")
    owner_email = f"changeplan-owner-{suffix}@example.com"
    _, auth = api("/api/v1/auth/register", {
        "organization_name": f"Change Plan E2E {suffix}",
        "email": owner_email, "password": OWNER_PASSWORD, "display_name": "Owner",
    }, expect=(201,))
    owner_token, tenant_id = auth["access_token"], auth["tenant_id"]

    _, site = api("/api/v1/tenant/sites", {"name": "Depot", "code": f"depot-{suffix}"}, owner_token, expect=(201,))
    site_id = site["id"]

    _, license_out = api("/api/v1/admin/licenses", {
        "tenant_id": tenant_id, "plan_code": f"small-{suffix}",
    }, admin_token, host="console.localhost", expect=(201,))
    old_license_id = license_out["id"]

    api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 1", "code": f"cam1-{suffix}"}, owner_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 2", "code": f"cam2-{suffix}"}, owner_token, expect=(201,))
    status, _ = api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 3", "code": f"cam3-{suffix}"}, owner_token, expect=(201, 402))
    check(status == 402, f"3rd camera is really refused under the small plan (got {status})", failures)

    step(3, "Call the real change-plan endpoint to move to the large plan")
    status, changed = api(
        f"/api/v1/admin/licenses/{old_license_id}/change-plan",
        {"new_plan_code": f"large-{suffix}"}, admin_token, host="console.localhost", expect=(200,),
    )
    check(status == 200, f"change-plan call succeeds (got {status})", failures)
    check(changed["plan_code"] == f"large-{suffix}", "response reports the new plan code", failures)
    new_license_id = changed["id"]

    step(4, "Confirm the real DB state: old license revoked, new license active under the new plan")
    old_status = psql(f"SELECT status FROM licenses WHERE id = '{old_license_id}'")
    check(old_status == "revoked", f"old license row is really revoked (got '{old_status}')", failures)
    new_status_and_plan = psql(
        f"SELECT l.status, p.code FROM licenses l JOIN license_plans p ON p.id = l.plan_id WHERE l.id = '{new_license_id}'"
    )
    check(
        new_status_and_plan == f"active|large-{suffix}",
        f"new license row is really active under the new plan (got '{new_status_and_plan}')", failures,
    )

    step(5, "Confirm the previously-refused 3rd camera can now really be created")
    status, _ = api("/api/v1/tenant/cameras", {"site_id": site_id, "name": "Cam 3", "code": f"cam3-{suffix}"}, owner_token, expect=(201, 402))
    check(status == 201, f"3rd camera now succeeds under the new plan's real quota row (got {status})", failures)

    step(6, "Clean up")
    tid = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tenant_id}';")
    if tid:
        psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{tid}';")
    for email in (owner_email, admin_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenant, admin, and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - license plan change verified for real: old license revoked, new license's own real quota enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against the live stack**

```bash
python scripts/e2e_license_change_plan.py
```
Expected: `PASS`.

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e_license_change_plan.py
git commit -m "e2e: verify admin-initiated license plan change against the real stack"
```

---

# Feature D: Reseller aggregate rollup

**The real cross-tenant problem, precisely** (see "Before you start" and the codebase's
own `reseller.py` docstring): `cameras`/`incidents`/`sites` all carry strict per-tenant
RLS. A reseller querying "how many cameras does each of my child tenants have" under its
own tenant session would get every child tenant back with a count of **zero**, silently —
not an error, just wrong — the exact failure mode this codebase already hit once on the
child-tenant *list* itself, fixed by never joining across the RLS boundary at all.
Aggregate counts, unlike that list, genuinely need to cross it. This task follows the
`edge_vpn_pool_snapshot()` / `support_grant_lookup()` precedent: a narrow, parameterized,
`SECURITY DEFINER` function that returns only aggregate counts — never row-level detail —
for child tenants a caller is already proven (by `organization_relationships`) to be the
reseller parent of.

### Task 1: Migration — the permission + the SECURITY DEFINER rollup function

**Files:**
- Create: `backend/migrations/versions/0056_reseller_rollup.py`

- [ ] **Step 1: Write the migration**

```python
"""Adds reseller.view_rollup (a new permission - a rollup is operational/business data,
not an audit trail, so it gets its own permission rather than reusing audit.read) and
reseller_child_tenant_rollup(), a narrow SECURITY DEFINER function crossing the RLS
boundary the same way edge_vpn_pool_snapshot() (migration 0029) and
support_grant_lookup() (migration 0051) already do.

Why this needs SECURITY DEFINER at all: cameras/incidents/sites all carry strict
per-tenant RLS (tenant_id = current_setting('app.tenant_id')::uuid, no exception for "I
am this tenant's reseller parent" - see this plan's own "Before you start" section). An
ordinary query for "how many cameras does each child tenant have", run under the
reseller's own tenant session, would return every child tenant with a count of zero -
not an error, silently wrong - the exact failure mode this codebase's own reseller.py
docstring already documents having hit once, on the child-tenant *list* itself (fixed
there by never crossing the RLS boundary; a rollup, unlike a list of tenant identities,
genuinely needs real numbers from inside each child tenant, so it can't take that same
"just don't join across it" escape).

The function takes a single parameter (the caller's own organization id, not
attacker-suppliable per-child ids) and returns only COUNTS - no incident titles, no
camera names, no anything that would leak a child tenant's actual operational detail to
its reseller parent beyond what organization_relationships already establishes they're
entitled to see exists. Every count is scoped by joining back through
organization_relationships WHERE parent_organization_id = the caller's own id AND
status = 'active' - the same authorization boundary _LIST_SQL (reseller.py) already
uses for the child-tenant list itself, so a rollup can never return more tenants than
the existing list endpoint would.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-18
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permissions (id, code, resource, action, risk_level, description) "
            "VALUES (:id, 'reseller.view_rollup', 'reseller', 'view_rollup', 'standard', "
            "'View an aggregate rollup across a reseller''s child tenants') "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"id": uuid.uuid4()},
    )
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' AND audience = 'customer'")
    ).scalar_one()
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE code = 'reseller.view_rollup'")
    ).scalar_one()
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id, effect) "
            "VALUES (:r, :p, 'allow') ON CONFLICT DO NOTHING"
        ),
        {"r": role_id, "p": permission_id},
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reseller_child_tenant_rollup(p_parent_organization_id uuid)
        RETURNS TABLE (
            tenant_id uuid,
            display_name text,
            tenant_status text,
            site_count bigint,
            camera_count bigint,
            active_incident_count bigint,
            license_status text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT
                t.id,
                o.display_name,
                t.status,
                (SELECT count(*) FROM sites s WHERE s.tenant_id = t.id AND s.deleted_at IS NULL),
                (SELECT count(*) FROM cameras c WHERE c.tenant_id = t.id AND c.deleted_at IS NULL),
                (
                    SELECT count(*) FROM incidents i
                    WHERE i.tenant_id = t.id
                      AND i.status IN ('open', 'acknowledged', 'investigating', 'escalated')
                ),
                (
                    SELECT l.status::text FROM licenses l
                    WHERE l.tenant_id = t.id AND l.status IN ('active', 'grace')
                    ORDER BY l.created_at DESC LIMIT 1
                )
            FROM organization_relationships rel
            JOIN organizations o ON o.id = rel.child_organization_id
            JOIN tenants t ON t.organization_id = o.id
            WHERE rel.parent_organization_id = p_parent_organization_id
              AND rel.status = 'active'
            ORDER BY t.created_at DESC
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION reseller_child_tenant_rollup(uuid) FROM PUBLIC")
    op.execute('GRANT EXECUTE ON FUNCTION reseller_child_tenant_rollup(uuid) TO "csense_api"')


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reseller_child_tenant_rollup(uuid)")
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE code = 'reseller.view_rollup')"
    )
    op.execute("DELETE FROM permissions WHERE code = 'reseller.view_rollup'")
```

**Note on the real incident-status values used above (already corrected in the code
block, this note records why)**: an earlier draft of this migration guessed `status NOT
IN ('closed', 'dismissed')`. Verified against `backend/tenant_api/app/api/incidents.py`
when this task actually ran — `ACTIVE_STATUSES = ("open", "acknowledged",
"investigating", "escalated")`, and the real `incidents.status` Postgres enum
(migration 0009) is `["open", "acknowledged", "investigating", "escalated",
"resolved", "dismissed"]` — **there is no `'closed'` value in the enum at all** (the
guessed literal would have failed outright, `invalid input value for enum
incident_status`), and the real "active" set also deliberately excludes `'resolved'`.
The code block above already reflects the corrected, verified set.

- [ ] **Step 2: Run the migration**

```bash
cd infra && docker compose --env-file ../.env build migrate && \
docker compose --env-file ../.env run --rm migrate
```
(this repo runs migrations via a dedicated one-shot `migrate` service, built from
`backend/migrations/Dockerfile` and COPYing the migrations directory at build time, not
a volume mount — a `build` before `run --rm` is required so the new migration file is
actually inside the image; confirmed the first time this plan's Task 1 ran, see that
task's own real output for the exact discovery.)

- [ ] **Step 3: Verify for real against live Postgres**

```bash
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT proname FROM pg_proc WHERE proname = 'reseller_child_tenant_rollup'"
docker compose --env-file ../.env exec -T postgres psql -U csense_app -d csense -tAc \
  "SELECT count(*) FROM permissions WHERE code = 'reseller.view_rollup'"
```
Expected: function name printed; count `1`.

- [ ] **Step 4: Commit**

```bash
git add backend/migrations/versions/0056_reseller_rollup.py
git commit -m "reseller: add reseller.view_rollup permission + SECURITY DEFINER rollup function"
```

### Task 2: The rollup endpoint

**Files:**
- Modify: `backend/tenant_api/app/api/reseller.py`

- [ ] **Step 1: Add the response model and endpoint**

```python
class ChildTenantRollupOut(BaseModel):
    tenant_id: str
    display_name: str
    tenant_status: str
    site_count: int
    camera_count: int
    active_incident_count: int
    license_status: str | None


class RollupSummaryOut(BaseModel):
    child_tenant_count: int
    total_sites: int
    total_cameras: int
    total_active_incidents: int
    tenants: list[ChildTenantRollupOut]


@router.get("/rollup", response_model=RollupSummaryOut)
async def get_child_tenant_rollup(
    context: TenantContext = Depends(current_tenant_context),
    db: AsyncSession = Depends(db_session_for_tenant),
) -> RollupSummaryOut:
    """Aggregate counts across every active child tenant - the real cross-RLS-boundary
    read _LIST_SQL's own docstring explains this endpoint's sibling (list_child_tenants)
    deliberately avoids. reseller_child_tenant_rollup() (migration 0056) is the narrow,
    parameterized SECURITY DEFINER function built for exactly this - see its own
    docstring in that migration for why an ordinary RLS-scoped query can't do this at
    all (it would return every child tenant with a real count of zero, silently)."""
    require_permission(context, "reseller.view_rollup")
    parent_organization_id = await _require_reseller_organization(db, context.tenant_id)

    rows = (
        await db.execute(
            text(
                "SELECT tenant_id, display_name, tenant_status, site_count, camera_count, "
                "active_incident_count, license_status "
                "FROM reseller_child_tenant_rollup(:parent_id)"
            ),
            {"parent_id": parent_organization_id},
        )
    ).all()

    tenants = [
        ChildTenantRollupOut(
            tenant_id=str(r[0]), display_name=r[1], tenant_status=r[2],
            site_count=r[3], camera_count=r[4], active_incident_count=r[5], license_status=r[6],
        )
        for r in rows
    ]
    return RollupSummaryOut(
        child_tenant_count=len(tenants),
        total_sites=sum(t.site_count for t in tenants),
        total_cameras=sum(t.camera_count for t in tenants),
        total_active_incidents=sum(t.active_incident_count for t in tenants),
        tenants=tenants,
    )
```
(placed after `list_child_tenants`, before `create_child_tenant` — router path
`/api/v1/tenant/child-tenants/rollup`; FastAPI resolves this correctly against the
existing `/{organization_id}`-shaped... note: `reseller.py` has no
`/{something}` path today, only `""` for list/create, so there's no path-ordering
conflict to worry about here, unlike routers that also declare a dynamic `/{id}` segment).

- [ ] **Step 2: Restart tenant-api**

```bash
cd infra && docker compose --env-file ../.env restart tenant-api
docker compose --env-file ../.env logs tenant-api --tail 30
```
Expected: no import/startup errors.

- [ ] **Step 3: Commit**

```bash
git add backend/tenant_api/app/api/reseller.py
git commit -m "reseller: add GET /child-tenants/rollup (real aggregate counts, cross-RLS via SECURITY DEFINER)"
```

### Task 3: Frontend — the rollup page

**Files:**
- Create: `frontend/customer-crm/src/api/reseller.ts`
- Create: `frontend/customer-crm/src/pages/ResellerRollupPage.tsx`
- Modify: `frontend/customer-crm/src/App.tsx` (or the project's actual router
  registration file — verify the real file at implementation time; `TeamPage.tsx`'s own
  route registration is the pattern to copy)

- [ ] **Step 1: Write the API client**

```typescript
import { apiFetch } from "./client";

/** Reseller aggregate rollup. Mirrors backend/tenant_api/app/api/reseller.py's
 *  GET /child-tenants/rollup. */

export interface ChildTenantRollup {
  tenant_id: string;
  display_name: string;
  tenant_status: string;
  site_count: number;
  camera_count: number;
  active_incident_count: number;
  license_status: string | null;
}

export interface RollupSummary {
  child_tenant_count: number;
  total_sites: number;
  total_cameras: number;
  total_active_incidents: number;
  tenants: ChildTenantRollup[];
}

export function getChildTenantRollup() {
  return apiFetch<RollupSummary>("/api/v1/tenant/child-tenants/rollup");
}
```

- [ ] **Step 2: Write the page**

Find the codebase's own existing loading/error/empty-state pattern first (used
throughout `TeamPage.tsx`/`RulesPage.tsx` — a `useResource`-style hook, or plain
`useState`/`useEffect` with a loading flag; match whichever this repo's own pages
actually use rather than introducing a second pattern) and follow it exactly. A
representative shape, assuming the same `useState`/`useEffect` idiom `TeamPage.tsx`
uses elsewhere in this same codebase:

```tsx
import { useEffect, useState } from "react";
import { getChildTenantRollup, type RollupSummary } from "../api/reseller";
import { ApiRequestError } from "../api/client";

export function ResellerRollupPage() {
  const [summary, setSummary] = useState<RollupSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    getChildTenantRollup()
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(
          err instanceof ApiRequestError
            ? err.body.message
            : "Could not load the reseller rollup.",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <p>Loading…</p>;
  if (error) return (
    <p role="alert" className="error-panel">
      {error}
    </p>
  );
  if (!summary) return null;

  return (
    <div>
      <h1>Child tenants</h1>
      <div className="summary-cards" style={{ display: "flex", gap: 16, marginBottom: 24 }}>
        <div className="card">
          <strong>{summary.child_tenant_count}</strong>
          <span>Child tenants</span>
        </div>
        <div className="card">
          <strong>{summary.total_sites}</strong>
          <span>Total sites</span>
        </div>
        <div className="card">
          <strong>{summary.total_cameras}</strong>
          <span>Total cameras</span>
        </div>
        <div className="card">
          <strong>{summary.total_active_incidents}</strong>
          <span>Active incidents</span>
        </div>
      </div>

      <table>
        <caption className="visually-hidden">Child tenants</caption>
        <thead>
          <tr>
            <th>Tenant</th>
            <th>Status</th>
            <th>Sites</th>
            <th>Cameras</th>
            <th>Active incidents</th>
            <th>License</th>
          </tr>
        </thead>
        <tbody>
          {summary.tenants.map((t) => (
            <tr key={t.tenant_id}>
              <td>{t.display_name}</td>
              <td>{t.tenant_status}</td>
              <td>{t.site_count}</td>
              <td>{t.camera_count}</td>
              <td>{t.active_incident_count}</td>
              <td>{t.license_status ?? "none"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {summary.tenants.length === 0 && <p className="muted">No child tenants yet.</p>}
    </div>
  );
}
```

- [ ] **Step 3: Register the route**

Locate the router config (check `App.tsx` first; if this codebase uses a different
routing file — e.g. a `routes.tsx` — register there instead) and add a route pointing
at `ResellerRollupPage`, following the exact same registration shape the existing
`TeamPage` route already uses (path, guard/permission check if the router does one,
nav-link entry if the app has a sidebar/nav list — `grep -rn "TeamPage"
frontend/customer-crm/src` finds every place `TeamPage` is referenced today; mirror
each one for `ResellerRollupPage`).

- [ ] **Step 4: Confirm the build compiles**

```bash
cd frontend/customer-crm && npm run build
```

- [ ] **Step 5: Commit**

```bash
git add frontend/customer-crm/src/api/reseller.ts frontend/customer-crm/src/pages/ResellerRollupPage.tsx \
        frontend/customer-crm/src/App.tsx
git commit -m "customer-crm: add reseller aggregate rollup page"
```

### Task 4: Verify for real — e2e script

**Files:**
- Create: `scripts/e2e_reseller_rollup.py`

- [ ] **Step 1: Write the script**

```python
"""End-to-end verification of the reseller aggregate rollup (migration 0056, GET
/api/v1/tenant/child-tenants/rollup). Proves against the real running stack, and
specifically proves the thing a naive cross-tenant query would get wrong:

  1. Bootstrap a real platform admin, create a real reseller organization/tenant
     (organization_type='reseller' - direct DB write, since there is no real API for
     provisioning a reseller itself yet, same as scripts/e2e_reseller.py's own approach).
  2. Through the real POST /child-tenants, create two real child tenants.
  3. Populate REAL data inside each child tenant under ITS OWN tenant session (a real
     site, real cameras, a real incident) - never inserted "as the reseller", proving
     the rollup crosses a genuine RLS boundary rather than reading data that happened to
     already be reseller-scoped.
  4. Call the real GET /child-tenants/rollup as the reseller and confirm the real
     per-tenant counts match what was actually created in step 3 - not zero, which is
     what a naive cross-tenant query under the reseller's own session would silently
     return (the exact failure this feature's migration docstring names).
  5. Confirm a non-reseller tenant's own call to the same endpoint gets a real
     403 not_a_reseller - the same guard list_child_tenants already enforces.

Run from the repo root with the stack up:
    python scripts/e2e_reseller_rollup.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid

import psycopg
from csense_shared.config import get_settings
from csense_shared.security.passwords import hash_password

API = "http://localhost:8080"
RESELLER_OWNER_PASSWORD = "RollupE2EReseller!Pass123"
CHILD_OWNER_PASSWORD = "RollupE2EChild!Pass456"


def api(path, payload=None, token=None, method="POST", expect=(200, 201, 204)):
    headers = {"Content-Type": "application/json", "Host": "app.localhost"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code in expect:
            return exc.code, (json.loads(body) if body else {})
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def psql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", "csense_app", "-d", "csense", "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if lines else ""


def bootstrap_reseller_owner(suffix: str) -> tuple[str, str, str]:
    """Direct DB write for the reseller organization itself (organization_type=
    'reseller') - mirrors scripts/e2e_reseller.py's own approach, since there is no real
    provisioning API for creating a reseller from scratch (only for a reseller creating
    ITS OWN children, which is exactly what this script exercises through the real API
    starting in step 2). Returns (email, tenant_id, organization_id)."""
    settings = get_settings()
    email = f"reseller-rollup-{suffix}@example.com"
    dsn = (
        f"host=localhost port=5432 dbname={settings.postgres_db} "
        f"user={settings.postgres_user} password={settings.postgres_password}"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.is_platform', 'true', false)")
        cur.execute(
            "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
            "VALUES ('reseller', %s, %s, %s, 'active') RETURNING id",
            (f"Reseller Rollup E2E {suffix}", f"Reseller Rollup E2E {suffix}", f"reseller-rollup-{suffix}"),
        )
        organization_id = cur.fetchone()[0]
        cur.execute("INSERT INTO tenants (organization_id, status) VALUES (%s, 'active') RETURNING id", (organization_id,))
        tenant_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (email_normalized, email_display, password_hash, status, display_name) "
            "VALUES (%s, %s, %s, 'active', 'Reseller Owner') RETURNING id",
            (email, email, hash_password(RESELLER_OWNER_PASSWORD, settings)),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM roles WHERE tenant_id IS NULL AND name = 'tenant_owner' AND audience = 'customer'"
        )
        role_id = cur.fetchone()[0]
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
        cur.execute(
            "INSERT INTO memberships (tenant_id, user_id, role_id, status, accepted_at) "
            "VALUES (%s, %s, %s, 'active', now())",
            (tenant_id, user_id, role_id),
        )
        conn.commit()
    return email, str(tenant_id), str(organization_id)


def redis_get_invitation_token(email: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis",
         "redis-cli", "--scan", "--pattern", "invite:*"],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    for key in result.stdout.strip().splitlines():
        value = subprocess.run(
            ["docker", "compose", "--env-file", "../.env", "exec", "-T", "redis", "redis-cli", "get", key],
            cwd="infra", capture_output=True, text=True, check=True,
        ).stdout.strip()
        if email in value:
            return key.split("invite:", 1)[1]
    raise RuntimeError(f"No invitation token found in Redis for {email}")


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    failures: list[str] = []

    step(1, "Bootstrap a real reseller organization/tenant/owner")
    reseller_email, reseller_tenant_id, _ = bootstrap_reseller_owner(suffix)
    _, reseller_auth = api("/api/v1/auth/login", {"email": reseller_email, "password": RESELLER_OWNER_PASSWORD})
    reseller_token = reseller_auth["access_token"]

    step(2, "Create two real child tenants through the real API")
    child_a_email = f"child-a-{suffix}@example.com"
    child_b_email = f"child-b-{suffix}@example.com"
    _, child_a = api("/api/v1/tenant/child-tenants", {
        "organization_name": f"Child A {suffix}", "owner_email": child_a_email, "owner_display_name": "Owner A",
    }, reseller_token, expect=(201,))
    _, child_b = api("/api/v1/tenant/child-tenants", {
        "organization_name": f"Child B {suffix}", "owner_email": child_b_email, "owner_display_name": "Owner B",
    }, reseller_token, expect=(201,))
    child_a_tenant_id, child_b_tenant_id = child_a["tenant_id"], child_b["tenant_id"]

    step(3, "Populate real data inside each child tenant, under its OWN tenant session")
    ticket_a = redis_get_invitation_token(child_a_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket_a, "password": CHILD_OWNER_PASSWORD}, expect=(200,))
    ticket_b = redis_get_invitation_token(child_b_email)
    api("/api/v1/auth/accept-invitation", {"token": ticket_b, "password": CHILD_OWNER_PASSWORD}, expect=(200,))

    _, child_a_auth = api("/api/v1/auth/login", {"email": child_a_email, "password": CHILD_OWNER_PASSWORD})
    child_a_token = child_a_auth["access_token"]
    _, site_a = api("/api/v1/tenant/sites", {"name": "Site", "code": f"a-{suffix}"}, child_a_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_a["id"], "name": "Cam 1", "code": f"a1-{suffix}"}, child_a_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_a["id"], "name": "Cam 2", "code": f"a2-{suffix}"}, child_a_token, expect=(201,))

    _, child_b_auth = api("/api/v1/auth/login", {"email": child_b_email, "password": CHILD_OWNER_PASSWORD})
    child_b_token = child_b_auth["access_token"]
    _, site_b = api("/api/v1/tenant/sites", {"name": "Site", "code": f"b-{suffix}"}, child_b_token, expect=(201,))
    api("/api/v1/tenant/cameras", {"site_id": site_b["id"], "name": "Cam 1", "code": f"b1-{suffix}"}, child_b_token, expect=(201,))

    step(4, "Call the real rollup as the reseller and confirm real, non-zero per-tenant counts")
    status, rollup = api("/api/v1/tenant/child-tenants/rollup", token=reseller_token, method="GET", expect=(200,))
    check(status == 200, f"rollup call succeeds (got {status})", failures)
    check(rollup["child_tenant_count"] == 2, f"rollup reports 2 child tenants (got {rollup.get('child_tenant_count')})", failures)
    check(rollup["total_cameras"] == 3, f"rollup's total_cameras is 3, the real sum across both children (got {rollup.get('total_cameras')})", failures)

    by_id = {t["tenant_id"]: t for t in rollup["tenants"]}
    check(
        child_a_tenant_id in by_id and by_id[child_a_tenant_id]["camera_count"] == 2,
        f"child A's own row reports camera_count=2, not silently zero (got {by_id.get(child_a_tenant_id, {}).get('camera_count')})",
        failures,
    )
    check(
        child_b_tenant_id in by_id and by_id[child_b_tenant_id]["camera_count"] == 1,
        f"child B's own row reports camera_count=1, not silently zero (got {by_id.get(child_b_tenant_id, {}).get('camera_count')})",
        failures,
    )
    check(
        by_id.get(child_a_tenant_id, {}).get("site_count") == 1 and by_id.get(child_b_tenant_id, {}).get("site_count") == 1,
        "both children report site_count=1 each", failures,
    )

    step(5, "Confirm a non-reseller tenant gets a real 403 not_a_reseller on the same endpoint")
    status, body = api("/api/v1/tenant/child-tenants/rollup", token=child_a_token, method="GET", expect=(200, 403))
    check(status == 403 and body.get("code") == "not_a_reseller", f"non-reseller call is refused with not_a_reseller (got {status}, {body.get('code')})", failures)

    step(6, "Clean up")
    for tid in (reseller_tenant_id, child_a_tenant_id, child_b_tenant_id):
        real = psql(f"SET app.is_platform = true; SELECT id FROM tenants WHERE id = '{tid}';")
        if real:
            psql(f"SET app.is_platform = true; DELETE FROM tenants WHERE id = '{real}';")
    for email in (reseller_email, child_a_email, child_b_email):
        psql(f"SET app.is_platform = true; DELETE FROM users WHERE email_normalized = '{email}';")
    print("    test tenants and users removed")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - reseller rollup verified for real: genuine per-child counts, not the silent-zero RLS trap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against the live stack**

```bash
python scripts/e2e_reseller_rollup.py
```
Expected: `PASS`.

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e_reseller_rollup.py
git commit -m "e2e: verify reseller aggregate rollup against the real stack (proves the RLS crossing is real)"
```

---

## After all four features: final verification and CHECKLIST.md

- [ ] Run the full backend pytest suite once more: `cd backend && python3 -m pytest
  tests/ -q` — expect the same pre-existing, unrelated `test_site_timezones.py` flake
  (see `CLAUDE.md`/this repo's own established baseline) and otherwise zero failures.
- [ ] Run all four new/touched e2e scripts once more in sequence, from the repo root:
  `scripts/e2e_finer_roles.py`, `scripts/e2e_site_scoping.py`,
  `scripts/e2e_license_change_plan.py`, `scripts/e2e_reseller_rollup.py`, plus a
  regression run of `scripts/e2e_memberships.py`, `scripts/e2e_licensing.py`, and
  `scripts/e2e_reseller.py` (all three touched files' existing behavior must be
  unchanged).
- [ ] Update `CHECKLIST.md`'s "Small, genuinely optional deferral" entry (the one
  naming WebAuthn/passkeys, per-site scoping UI, finer role granularity, license
  upgrade/downgrade flow, reseller aggregate rollups) — move the four items this plan
  covers from that deferred list into the shipped-and-verified section, following the
  same rigor and real-evidence style as the zone-privacy-level-gating entry added
  earlier this session (real e2e output, real test counts, the real behavior-change
  decision documented, not glossed over).
- [ ] Commit the `CHECKLIST.md` update on its own.
