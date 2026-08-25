#!/usr/bin/env sh
# Bootstrap the non-superuser application role, then apply migrations.
# Order matters: migration 0004 grants privileges to that role and refuses to run if it
# doesn't exist or could bypass RLS.
set -e

python /app/migrations/bootstrap_roles.py
exec alembic upgrade head
