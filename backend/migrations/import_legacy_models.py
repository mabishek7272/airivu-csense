"""Imports the legacy CSense model estate into the model registry.

    python import_legacy_models.py --source /path/to/staged/models [--dry-run]

For each model in legacy_model_manifest.py this:
  1. verifies the local file's SHA-256 against the manifest (refusing on mismatch);
  2. uploads it to the content-addressed key in the `csense-models` bucket;
  3. records a `stored_objects` row and an immutable `model_versions` row.

Idempotent. Re-running skips artifacts already uploaded and already registered, so a
partial run can simply be repeated — matching the migration controls in
docs/06_IMPLEMENTATION_PLAN.md §11.2 ("idempotent import with source ID mapping").

Runs as the owner role because it creates platform-global registry rows; it is an
operator tool, not a request path.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from legacy_model_manifest import LEGACY_MODELS, LegacyModel

from csense_shared.config import get_settings
from csense_shared.storage.objects import (
    BUCKET_MODELS,
    create_client,
    ensure_buckets,
    sha256_file,
    upload_model_artifact,
)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "csense")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _upsert_model(cur, model: LegacyModel) -> str:
    cur.execute("SELECT id FROM models WHERE name = %s", (model.model_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO models (name, task_code, description, owner_team, status, default_label_schema)
        VALUES (%s, %s, %s, 'legacy-migration', 'active', %s)
        RETURNING id
        """,
        (model.model_name, model.task_code, model.description, psycopg.types.json.Json(model.label_map)
         if model.label_map else None),
    )
    return cur.fetchone()[0]


def _register_version(cur, model: LegacyModel, model_id: str, upload, local_path: Path) -> str | None:
    """Returns the new model_version id, or None if this artifact was already registered."""
    cur.execute("SELECT id, state FROM model_versions WHERE artifact_sha256 = %s", (model.sha256,))
    existing = cur.fetchone()
    if existing:
        print(f"    already registered (state={existing[1]})")
        return None

    cur.execute(
        """
        INSERT INTO stored_objects
            (tenant_id, bucket, object_key, object_type, mime_type, size_bytes, sha256, retention_class)
        VALUES (NULL, %s, %s, 'model_artifact', 'application/octet-stream', %s, %s, 'permanent')
        ON CONFLICT (bucket, object_key) DO UPDATE SET sha256 = EXCLUDED.sha256
        RETURNING id
        """,
        (upload.bucket, upload.object_key, upload.size_bytes, upload.sha256),
    )
    object_id = cur.fetchone()[0]

    provenance = {
        "origin": "legacy_csense_migration",
        "legacy_host": "103.118.158.92",
        "legacy_paths": list(model.legacy_paths),
        "legacy_detection_classes": list(model.legacy_classes),
        "original_filename": local_path.name,
        "duplicate_path_count": len(model.legacy_paths),
        "notes": model.notes or None,
    }

    cur.execute(
        """
        INSERT INTO model_versions (
            model_id, version_label, artifact_object_id, artifact_sha256,
            framework, runtime, label_map, hardware_profile, license_metadata, provenance,
            access_classification, state, state_reason
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            model_id,
            model.version_label,
            object_id,
            model.sha256,
            model.framework,
            model.runtime,
            psycopg.types.json.Json(model.label_map) if model.label_map else None,
            psycopg.types.json.Json(model.hardware_profile or {}),
            psycopg.types.json.Json(model.license_metadata),
            psycopg.types.json.Json(provenance),
            model.access_classification,
            model.initial_state,
            model.state_reason,
        ),
    )
    version_id = cur.fetchone()[0]

    # A version that lands anywhere other than the class default ('uploaded') changed
    # state without ever going through the real, audited /model-versions/{id}/promote
    # endpoint - this import script *is* that state change, so it has to leave the same
    # trail that endpoint would have, or test_every_biometric_promotion_is_audited's own
    # question ("whatever state these end up in, the path there must be reconstructable")
    # goes unanswered. Found missing 2026-09-08, well after the first real import - the
    # gap was real and latent, not hypothetical.
    if model.initial_state != "uploaded":
        cur.execute(
            """
            INSERT INTO audit_events
                (tenant_id, actor_type, actor_id, action, target_type, target_id, outcome,
                 reason, before_patch, after_patch)
            VALUES (NULL, 'system', %s, 'model.promote', 'model_version', %s, 'success',
                    %s, %s, %s)
            """,
            (
                "import_legacy_models.py",
                str(version_id),
                model.state_reason or f"Registered directly at '{model.initial_state}' by the import script.",
                psycopg.types.json.Json({"state": "uploaded"}),
                psycopg.types.json.Json({"state": model.initial_state}),
            ),
        )

    return version_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Directory holding the staged model files")
    parser.add_argument("--dry-run", action="store_true", help="Verify digests only; no uploads or writes")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        print(f"Source directory not found: {source}", file=sys.stderr)
        raise SystemExit(1)

    settings = get_settings()
    minio = create_client(settings)

    if not args.dry_run:
        created = ensure_buckets(minio)
        if created:
            print(f"Created buckets: {', '.join(created)}\n")

    # Verify every artifact before writing anything, so a bad file fails the run early
    # rather than leaving a half-populated registry.
    verified: list[tuple[LegacyModel, Path]] = []
    problems: list[str] = []
    for model in LEGACY_MODELS:
        path = source / model.local_name
        if not path.exists():
            problems.append(f"missing file: {model.local_name}")
            continue
        actual = sha256_file(path)
        if actual != model.sha256:
            problems.append(
                f"digest mismatch for {model.local_name}: manifest {model.sha256[:12]}..., file {actual[:12]}..."
            )
            continue
        verified.append((model, path))

    if problems:
        print("Refusing to import; artifact verification failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Verified {len(verified)} artifacts against the manifest.\n")
    if args.dry_run:
        for model, path in verified:
            size_mb = path.stat().st_size / 1048576
            print(f"  {model.model_name:38s} {size_mb:7.1f} MB  {model.initial_state:10s} {model.access_classification}")
        print("\nDry run complete; nothing written.")
        return

    imported = skipped = 0
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        for model, path in verified:
            print(f"  {model.model_name} ({model.version_label})")
            upload = upload_model_artifact(
                minio,
                local_path=path,
                model_name=model.model_name,
                version_label=model.version_label,
                extension=path.suffix,
            )
            print(
                f"    artifact {'already in' if upload.already_present else 'uploaded to'} "
                f"{BUCKET_MODELS}/{upload.object_key}"
            )
            model_id = _upsert_model(cur, model)
            version_id = _register_version(cur, model, model_id, upload, path)
            if version_id is None:
                skipped += 1
            else:
                imported += 1
                flag = "  [BIOMETRIC - quarantined]" if model.access_classification == "biometric" else ""
                print(f"    registered version {version_id} state={model.initial_state}{flag}")
        conn.commit()

    print(f"\nImport complete: {imported} newly registered, {skipped} already present.")
    biometric = [m for m, _ in verified if m.access_classification == "biometric"]
    if biometric:
        print(
            f"\n{len(biometric)} biometric model(s) were registered in the 'revoked' state and "
            "cannot be assigned to a pipeline until explicitly promoted. See CLARIFICATIONS.md."
        )


if __name__ == "__main__":
    main()
