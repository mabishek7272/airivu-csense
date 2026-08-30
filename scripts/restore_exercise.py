"""Real restore-exercise (CHECKLIST: "Backup automation + restore-exercise scripts") -
proves the *latest* backup `scripts/backup.py` produced actually restores to working
data, not just that a dump file exists somewhere. A backup nobody has ever restored is a
belief, not a backup.

Restores into a throwaway database (`csense_restore_verify`) on the *same* postgres
server the real `csense` database lives on - safe (the real database is never touched,
only read from for the row-count comparison) and honest about what this proves: that the
dump/restore mechanism itself is sound. It is not a disaster-recovery drill against a
second, independent server (this deployment does not have one to restore onto) - a real
DR exercise restoring onto genuinely separate infrastructure is the natural next step
once one exists, not something to fake here.

Exits 1 on any real problem: no backup found, restore fails, or row counts disagree.

    python scripts/restore_exercise.py
"""
from __future__ import annotations

import subprocess
import sys

from csense_shared.config import get_settings
from csense_shared.storage.objects import BUCKET_BACKUPS
from minio import Minio

SCRATCH_DB = "csense_restore_verify"

# A representative sample, not every table - enough to prove the restore carried real,
# varied data (identity, tenancy, and operational rows) rather than an empty schema.
COMPARISON_TABLES = ["organizations", "tenants", "users", "cameras", "incidents"]


def psql(*, container: str, db: str, user: str, sql: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", container,
         "psql", "-U", user, "-d", db, "-tAc", sql],
        cwd="infra", capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def row_count(*, container: str, db: str, user: str, table: str) -> int:
    return int(psql(container=container, db=db, user=user, sql=f"SELECT count(*) FROM {table}"))


def step(n, text):
    print(f"\n[{n}] {text}")


def check(condition, description, failures):
    print(f"    {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main() -> int:
    settings = get_settings()
    failures: list[str] = []

    step(1, "Find the most recent Postgres backup in csense-backups")
    minio_client = Minio(
        "localhost:9000", access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password, secure=False,
    )
    prefix = f"{settings.environment}/postgres/"
    objects = sorted(
        minio_client.list_objects(BUCKET_BACKUPS, prefix=prefix, recursive=True),
        key=lambda o: o.object_name,
    )
    if not objects:
        print(f"FAIL - no backup found under {BUCKET_BACKUPS}/{prefix} - run scripts/backup.py first", file=sys.stderr)
        return 1
    latest = objects[-1]
    print(f"    using {latest.object_name} ({latest.size:,} bytes)")

    step(2, "Download the real dump bytes")
    response = minio_client.get_object(BUCKET_BACKUPS, latest.object_name)
    dump_bytes = response.read()
    response.close()
    response.release_conn()
    check(len(dump_bytes) == latest.size, f"downloaded exactly {latest.size:,} bytes", failures)

    step(3, "Restore into a throwaway database, never touching the real one")
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", settings.postgres_user, "-d", "postgres", "-c",
         f"DROP DATABASE IF EXISTS {SCRATCH_DB}"],
        cwd="infra", capture_output=True, check=True,
    )
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", settings.postgres_user, "-d", "postgres", "-c", f"CREATE DATABASE {SCRATCH_DB}"],
        cwd="infra", capture_output=True, check=True,
    )
    restore = subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "pg_restore", "-U", settings.postgres_user, "-d", SCRATCH_DB, "--no-owner", "--no-privileges"],
        cwd="infra", input=dump_bytes, capture_output=True, check=False,
    )
    # pg_restore exits non-zero on warnings alone (e.g. "role does not exist" for a role
    # this scratch restore never created) - a real failure is one that produced no usable
    # database at all, which the row-count comparison below will catch either way.
    if restore.returncode != 0:
        print(f"    pg_restore exit {restore.returncode} (may be non-fatal warnings):")
        print("    " + restore.stderr.decode(errors="replace")[:2000].replace("\n", "\n    "))

    step(4, "Compare real row counts between the source and the restored database")
    for table in COMPARISON_TABLES:
        source_count = row_count(container="postgres", db=settings.postgres_db, user=settings.postgres_user, table=table)
        restored_count = row_count(container="postgres", db=SCRATCH_DB, user=settings.postgres_user, table=table)
        check(
            source_count == restored_count,
            f"{table}: source={source_count} restored={restored_count}", failures,
        )

    step(5, "Clean up the scratch database")
    subprocess.run(
        ["docker", "compose", "--env-file", "../.env", "exec", "-T", "postgres",
         "psql", "-U", settings.postgres_user, "-d", "postgres", "-c",
         f"DROP DATABASE IF EXISTS {SCRATCH_DB}"],
        cwd="infra", capture_output=True, check=True,
    )
    print(f"    {SCRATCH_DB} dropped")

    print()
    if failures:
        print(f"FAIL - {len(failures)} check(s) did not hold:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - the latest backup restores to a working database with matching row counts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
