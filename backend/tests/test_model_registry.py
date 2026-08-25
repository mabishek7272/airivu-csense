"""Model registry guarantees (docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §15,
docs/05_BACKEND_SCHEMA.md §8, §19).

These lock in the properties the AI control plane depends on: a published version can
never be repointed at different bytes, identical bytes cannot be registered twice, and
biometric artifacts cannot sit in a deployable state without someone explicitly moving
them there.

Needs a migrated database (TEST_POSTGRES_DSN); skipped otherwise.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set — skipping"
)


def _owner_conn() -> psycopg.Connection:
    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"], autocommit=True)


@pytest.fixture()
def registered_version():
    """Creates a throwaway model + version, and cleans it up afterwards."""
    digest = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars
    suffix = uuid.uuid4().hex[:8]
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO models (name, task_code, description) VALUES (%s, 'object_detection', 'test') "
            "RETURNING id",
            (f"test-model-{suffix}",),
        )
        model_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO stored_objects (bucket, object_key, object_type, size_bytes, sha256) "
            "VALUES ('csense-models', %s, 'model_artifact', 123, %s) RETURNING id",
            (f"global/models/test-{suffix}/v1/{digest}.onnx", digest),
        )
        object_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO model_versions (model_id, version_label, artifact_object_id, artifact_sha256, "
            "framework, runtime) VALUES (%s, 'v1', %s, %s, 'onnx', 'onnxruntime') RETURNING id",
            (model_id, object_id, digest),
        )
        version_id = cur.fetchone()[0]

    yield {"model_id": model_id, "version_id": version_id, "object_id": object_id, "sha256": digest}

    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM model_versions WHERE id = %s", (version_id,))
        cur.execute("DELETE FROM stored_objects WHERE id = %s", (object_id,))
        cur.execute("DELETE FROM models WHERE id = %s", (model_id,))


def test_published_version_artifact_cannot_be_repointed(registered_version):
    """TRD §15: artifacts are content-addressed and cannot be overwritten. Swapping the
    digest on an existing version would let a reviewed version silently start serving
    different weights."""
    other_digest = uuid.uuid4().hex + uuid.uuid4().hex
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute(
                "UPDATE model_versions SET artifact_sha256 = %s WHERE id = %s",
                (other_digest, registered_version["version_id"]),
            )


def test_version_label_and_model_are_immutable(registered_version):
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation):
            cur.execute(
                "UPDATE model_versions SET version_label = 'v2' WHERE id = %s",
                (registered_version["version_id"],),
            )


def test_state_transitions_are_still_allowed(registered_version):
    """Immutability must not freeze the lifecycle — promotion/rollback are state changes."""
    with _owner_conn() as conn, conn.cursor() as cur:
        for state in ("validating", "validated", "staging", "production", "deprecated"):
            cur.execute(
                "UPDATE model_versions SET state = %s WHERE id = %s RETURNING state",
                (state, registered_version["version_id"]),
            )
            assert cur.fetchone()[0] == state


def test_identical_bytes_cannot_be_registered_twice(registered_version):
    """Content addressing: one blob, one version. Prevents the legacy estate's problem of
    the same weights living under several names."""
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO model_versions (model_id, version_label, artifact_object_id, "
                "artifact_sha256, framework, runtime) VALUES (%s, 'duplicate', %s, %s, 'onnx', 'onnxruntime')",
                (
                    registered_version["model_id"],
                    registered_version["object_id"],
                    registered_version["sha256"],
                ),
            )


def test_malformed_digest_rejected(registered_version):
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO model_versions (model_id, version_label, artifact_object_id, "
                "artifact_sha256, framework, runtime) "
                "VALUES (%s, 'bad-digest', %s, 'not-a-sha256', 'onnx', 'onnxruntime')",
                (registered_version["model_id"], registered_version["object_id"]),
            )


def test_invalid_access_classification_rejected(registered_version):
    with _owner_conn() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "UPDATE model_versions SET access_classification = 'anything-goes' WHERE id = %s",
                (registered_version["version_id"],),
            )


def test_imported_biometric_models_are_not_deployable():
    """The InsightFace set must not sit in a state a pipeline could pick up. Facial
    recognition is a release-one non-goal (PRD), so these stay quarantined until somebody
    deliberately promotes them."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.name, mv.state FROM model_versions mv JOIN models m ON m.id = mv.model_id "
            "WHERE mv.access_classification = 'biometric'"
        )
        rows = cur.fetchall()
        if not rows:
            pytest.skip("legacy models not imported into this database")

        deployable = {"validated", "staging", "production"}
        offenders = [(name, state) for name, state in rows if state in deployable]
        assert not offenders, f"biometric models in a deployable state: {offenders}"


def test_imported_legacy_models_carry_license_and_provenance():
    """Licence terms and origin must travel with the artifact — several legacy weights are
    AGPL-3.0 or non-commercial, which constrains how a hosted service may use them."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.name, mv.license_metadata, mv.provenance FROM model_versions mv "
            "JOIN models m ON m.id = mv.model_id "
            "WHERE mv.provenance->>'origin' = 'legacy_csense_migration'"
        )
        rows = cur.fetchall()
        if not rows:
            pytest.skip("legacy models not imported into this database")

        for name, license_metadata, provenance in rows:
            assert license_metadata and license_metadata.get("license"), f"{name} has no licence recorded"
            assert provenance.get("legacy_paths"), f"{name} has no legacy source paths recorded"
