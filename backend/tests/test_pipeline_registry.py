"""Pipeline registry guarantees (docs/05_BACKEND_SCHEMA.md §8.4, §8.5, §8.8; migration
0031's own trigger and constraints).

Mirrors test_model_registry.py's shape closely - a pipeline version is immutable after
creation the same way a model version is, for the same reason (SCH §19: "Model/pipeline
published versions cannot be updated; only state transitions and new versions are
allowed"). What's specific to pipelines here: the digest is over `definition_json`, not
an artifact's bytes, and `pipeline_assignments` is tenant-owned, so it also gets the
overlap constraint and an RLS check the model registry has no equivalent of.

Needs a migrated database (TEST_POSTGRES_DSN); skipped otherwise.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set — skipping"
)


def _owner_conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"], autocommit=True)


def _digest(definition: dict) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture()
def registered_version():
    """A throwaway pipeline + draft version, cleaned up afterwards."""
    suffix = uuid.uuid4().hex[:8]
    definition = {"stages": [{"type": "infer", "model_name": f"test-model-{suffix}", "min_model_state": "validated"}]}
    digest = _digest(definition)

    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipelines (code, name, use_case) VALUES (%s, %s, 'test') RETURNING id",
            (f"test-pipeline-{suffix}", f"Test Pipeline {suffix}"),
        )
        pipeline_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO pipeline_versions (pipeline_id, version_number, definition_json, definition_sha256) "
            "VALUES (%s, 1, %s, %s) RETURNING id",
            (pipeline_id, json.dumps(definition), digest),
        )
        version_id = cur.fetchone()[0]

    yield {"pipeline_id": pipeline_id, "version_id": version_id, "definition": definition, "digest": digest}

    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pipeline_assignments WHERE pipeline_version_id = %s", (version_id,))
        cur.execute("DELETE FROM pipeline_versions WHERE pipeline_id = %s", (pipeline_id,))
        cur.execute("DELETE FROM pipelines WHERE id = %s", (pipeline_id,))


def test_published_definition_cannot_be_repointed(registered_version):
    """The property this whole trigger exists for: a version someone reviewed and
    published must not be able to silently start meaning something else."""
    other = {"stages": [{"type": "infer", "model_name": "different-model", "min_model_state": "validated"}]}
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute(
                "UPDATE pipeline_versions SET definition_json = %s WHERE id = %s",
                (json.dumps(other), registered_version["version_id"]),
            )


def test_version_number_and_pipeline_are_immutable(registered_version):
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute(
                "UPDATE pipeline_versions SET version_number = 2 WHERE id = %s",
                (registered_version["version_id"],),
            )


def test_state_transitions_are_still_allowed(registered_version):
    """Immutability must not freeze the lifecycle - draft -> published -> deprecated are
    state changes, not definition changes."""
    with _owner_conn() as conn, conn.cursor() as cur:
        for state in ("published", "deprecated"):
            cur.execute(
                "UPDATE pipeline_versions SET state = %s WHERE id = %s RETURNING state",
                (state, registered_version["version_id"]),
            )
            assert cur.fetchone()[0] == state


def test_identical_definition_cannot_be_registered_twice(registered_version):
    """Content addressing over the definition: the same stage list under a second
    version_number would be indistinguishable from the first at execution time, so it's
    refused at creation instead."""
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO pipeline_versions (pipeline_id, version_number, definition_json, definition_sha256) "
                "VALUES (%s, 2, %s, %s)",
                (
                    registered_version["pipeline_id"],
                    json.dumps(registered_version["definition"]),
                    registered_version["digest"],
                ),
            )


def test_malformed_digest_rejected(registered_version):
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO pipeline_versions (pipeline_id, version_number, definition_json, definition_sha256) "
                "VALUES (%s, 2, %s, 'not-a-sha256')",
                (registered_version["pipeline_id"], json.dumps({"stages": []})),
            )


def test_pipeline_code_must_be_unique(registered_version):
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code FROM pipelines WHERE id = %s", (registered_version["pipeline_id"],))
        code = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO pipelines (code, name, use_case) VALUES (%s, 'dup', 'test')", (code,))


@pytest.fixture()
def tenant_with_camera(registered_version):
    """A throwaway tenant + site + camera, plus the published version from
    `registered_version` (assignments require a published target)."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pipeline_versions SET state = 'published' WHERE id = %s",
            (registered_version["version_id"],),
        )
        suffix = uuid.uuid4().hex[:8]
        cur.execute(
            "INSERT INTO organizations (organization_type, legal_name, display_name, slug, status) "
            "VALUES ('direct_customer', %s, %s, %s, 'active') RETURNING id",
            (f"Pipeline Reg Test {suffix}", f"Pipeline Reg Test {suffix}", f"pipeline-reg-test-{suffix}"),
        )
        org_id = cur.fetchone()[0]
        cur.execute("INSERT INTO tenants (organization_id, status) VALUES (%s, 'active') RETURNING id", (org_id,))
        tenant_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sites (tenant_id, name, code) VALUES (%s, 'Test Site', %s) RETURNING id",
            (tenant_id, f"site-{suffix}"),
        )
        site_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO cameras (tenant_id, site_id, name, code, status) "
            "VALUES (%s, %s, 'Test Camera', %s, 'ready') RETURNING id",
            (tenant_id, site_id, f"cam-{suffix}"),
        )
        camera_id = cur.fetchone()[0]

    yield {**registered_version, "tenant_id": tenant_id, "camera_id": camera_id}

    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        cur.execute("DELETE FROM organizations WHERE id = %s", (org_id,))


def test_second_active_assignment_at_same_priority_is_rejected(tenant_with_camera):
    """SCH §8.8: 'prevents overlapping active assignment for the same (camera_id,
    pipeline/use_case, priority)'. Two live assignments racing for the same priority slot
    on one camera would make 'which pipeline is this camera actually running' ambiguous."""
    ctx = tenant_with_camera
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_assignments (tenant_id, camera_id, pipeline_version_id, priority) "
            "VALUES (%s, %s, %s, 100)",
            (ctx["tenant_id"], ctx["camera_id"], ctx["version_id"]),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO pipeline_assignments (tenant_id, camera_id, pipeline_version_id, priority) "
                "VALUES (%s, %s, %s, 100)",
                (ctx["tenant_id"], ctx["camera_id"], ctx["version_id"]),
            )


def test_a_revoked_assignment_frees_the_priority_slot(tenant_with_camera):
    ctx = tenant_with_camera
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_assignments (tenant_id, camera_id, pipeline_version_id, priority) "
            "VALUES (%s, %s, %s, 100) RETURNING id",
            (ctx["tenant_id"], ctx["camera_id"], ctx["version_id"]),
        )
        first_id = cur.fetchone()[0]
        cur.execute("UPDATE pipeline_assignments SET status = 'revoked' WHERE id = %s", (first_id,))
        # Should not raise now that the first is no longer active.
        cur.execute(
            "INSERT INTO pipeline_assignments (tenant_id, camera_id, pipeline_version_id, priority) "
            "VALUES (%s, %s, %s, 100)",
            (ctx["tenant_id"], ctx["camera_id"], ctx["version_id"]),
        )


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_API_DSN"), reason="TEST_POSTGRES_API_DSN not set — skipping"
)
def test_tenant_cannot_read_another_tenants_pipeline_assignments(tenant_with_camera):
    """The property migration 0031's own RLS policy exists for - written by hand for this
    migration specifically, so a typo in it (unlike the generic memberships/sites checks
    in test_tenant_isolation.py) needs its own proof."""
    ctx = tenant_with_camera
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_assignments (tenant_id, camera_id, pipeline_version_id) VALUES (%s, %s, %s)",
            (ctx["tenant_id"], ctx["camera_id"], ctx["version_id"]),
        )

    with psycopg.connect(os.environ["TEST_POSTGRES_API_DSN"], autocommit=True) as conn, conn.cursor() as cur:
        # A different, unrelated tenant scope - not ctx["tenant_id"].
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(uuid.uuid4()),))
        cur.execute("SELECT set_config('app.is_platform', 'false', false)")
        cur.execute(
            "SELECT count(*) FROM pipeline_assignments WHERE tenant_id = %s", (ctx["tenant_id"],)
        )
        assert cur.fetchone()[0] == 0, "another tenant's scope must not see this assignment"
