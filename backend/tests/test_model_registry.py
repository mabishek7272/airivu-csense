"""Model registry guarantees (docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md §15,
docs/05_BACKEND_SCHEMA.md §8, §19).

These lock in the properties the AI control plane depends on: a published version can
never be repointed at different bytes, identical bytes cannot be registered twice, and
face-processing models stay labelled biometric with an auditable promotion history.

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


def test_biometric_models_keep_their_classification():
    """Biometric models were promoted to production by owner decision (CLARIFICATIONS #16),
    so state is no longer the control. What must still hold is that they remain *labelled*
    biometric: the classification is how a privacy review, a data-subject request, or an
    incident responder finds every face-processing model in one query. Silently relabelling
    one 'standard' would make it invisible to all of those."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.name FROM model_versions mv JOIN models m ON m.id = mv.model_id "
            "WHERE m.task_code IN ('face_detection', 'face_recognition', 'face_landmark', "
            "'face_attribute') AND mv.access_classification <> 'biometric'"
        )
        mislabelled = [row[0] for row in cur.fetchall()]
        assert not mislabelled, f"face-processing models not classified biometric: {mislabelled}"


def test_every_biometric_promotion_is_audited():
    """Whatever state these end up in, the path there must be reconstructable."""
    with _owner_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT mv.id, m.name FROM model_versions mv JOIN models m ON m.id = mv.model_id "
            "WHERE mv.access_classification = 'biometric' AND mv.state <> 'uploaded'"
        )
        rows = cur.fetchall()
        if not rows:
            pytest.skip("no biometric models in this database")

        for version_id, name in rows:
            cur.execute(
                "SELECT count(*) FROM audit_events WHERE action = 'model.promote' AND target_id = %s",
                (str(version_id),),
            )
            assert cur.fetchone()[0] > 0, f"{name} changed state with no audit record"


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
