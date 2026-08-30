"""Real backup automation (CHECKLIST: "Backup automation + restore-exercise scripts
(automatable against local MinIO/PG/Mongo)" - MongoDB is no longer part of this stack,
CLARIFICATIONS #19/#20; Postgres and MinIO are what actually needs backing up now).

Two things happen, both real, both landing in the `csense-backups` bucket at the exact
key layout SCH §14 already specifies (`{environment}/{store}/{date}/{artifact}`) - this
bucket has existed in `csense_shared.storage.objects.ALL_BUCKETS` since MinIO was first
wired up, unused until this script:

1. **Postgres**: a real `pg_dump -Fc` (custom format - compressed, and selectively
   restorable, unlike a plain SQL dump) taken *inside* the postgres container via
   `docker compose exec`, so the dump and any future restore always use the exact same
   server version - no host-side `pg_dump` binary, no version-skew risk.
2. **MinIO**: not "backed up into itself" (a bucket cannot meaningfully back up its own
   sibling buckets by copying within the same instance - that protects against nothing a
   real outage would take out) - a real object inventory (key, size, ETag) of every
   other bucket, uploaded as a manifest. This is the honest scope for a *local* exercise:
   real off-site replication needs a genuine second storage target, which this
   deployment does not have. The manifest is still real, useful work on its own - it is
   exactly what a `restore_exercise.py` or a future `mc mirror` job would diff against to
   know whether replication actually caught everything.

    python scripts/backup.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import subprocess
import sys

from csense_shared.config import get_settings
from csense_shared.storage.objects import ALL_BUCKETS, BUCKET_BACKUPS, ensure_buckets
from minio import Minio


def pg_dump(*, container: str, db: str, user: str) -> bytes:
    """Runs pg_dump inside the real postgres container and returns the dump bytes -
    never touches the host's own pg_dump (there may not be one), and never risks a
    version mismatch between what took the dump and what would restore it."""
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", container,
         "pg_dump", "-U", user, "-Fc", db],
        cwd="infra", capture_output=True, check=True,
    )
    return result.stdout


def minio_manifest(client: Minio) -> dict:
    """A real object inventory (not simulated) for every bucket except csense-backups
    itself - key, size, and ETag for each object, the same fields a real replication job
    would diff against to confirm nothing was missed."""
    manifest: dict = {"buckets": {}}
    for bucket in ALL_BUCKETS:
        if bucket == BUCKET_BACKUPS:
            continue
        objects = []
        total_bytes = 0
        for obj in client.list_objects(bucket, recursive=True):
            objects.append({"key": obj.object_name, "size": obj.size, "etag": obj.etag})
            total_bytes += obj.size or 0
        manifest["buckets"][bucket] = {
            "object_count": len(objects), "total_bytes": total_bytes, "objects": objects,
        }
    return manifest


def main() -> int:
    settings = get_settings()
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    print("[1] Ensuring the backup bucket exists")
    minio_client = Minio(
        "localhost:9000", access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password, secure=False,
    )
    ensure_buckets(minio_client)

    print("[2] Taking a real pg_dump from the running postgres container")
    dump_bytes = pg_dump(container="postgres", db=settings.postgres_db, user=settings.postgres_user)
    if not dump_bytes:
        print("FAIL - pg_dump produced no output", file=sys.stderr)
        return 1
    dump_sha256 = hashlib.sha256(dump_bytes).hexdigest()
    dump_key = f"{settings.environment}/postgres/{today}/csense-{timestamp}.dump"
    minio_client.put_object(
        BUCKET_BACKUPS, dump_key, io.BytesIO(dump_bytes), length=len(dump_bytes),
        content_type="application/octet-stream",
    )
    print(f"    {len(dump_bytes):,} bytes -> {BUCKET_BACKUPS}/{dump_key}")
    print(f"    sha256: {dump_sha256}")

    print("[3] Building a real MinIO object inventory manifest")
    manifest = minio_manifest(minio_client)
    manifest["taken_at"] = dt.datetime.now(dt.UTC).isoformat()
    manifest["postgres_dump_key"] = dump_key
    manifest["postgres_dump_sha256"] = dump_sha256
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    manifest_key = f"{settings.environment}/minio/{today}/manifest-{timestamp}.json"
    minio_client.put_object(
        BUCKET_BACKUPS, manifest_key, io.BytesIO(manifest_bytes), length=len(manifest_bytes),
        content_type="application/json",
    )
    for bucket, info in manifest["buckets"].items():
        print(f"    {bucket}: {info['object_count']} objects, {info['total_bytes']:,} bytes")
    print(f"    manifest -> {BUCKET_BACKUPS}/{manifest_key}")

    print("\nPASS - real Postgres dump and MinIO inventory manifest both written to csense-backups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
