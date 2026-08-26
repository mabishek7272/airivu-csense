"""Promotes the migrated legacy models to a deployable state.

These models have run in the legacy production platform since 2022, so the owner has
accepted them as production-proven and directed that all of them - including the
biometric set - be made available to the runtime.

The promotion still walks the real state machine one transition at a time rather than
writing `production` directly, so each step lands in `audit_events` with a reason and a
`model.version.state.changed.v1` outbox event. The point is not to slow anyone down; it
is that six months from now the record shows exactly when each model became deployable
and on whose authority.

    python promote_legacy_models.py [--target production] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

# Mirrors VALID_TRANSITIONS in admin_api/app/api/models.py. Kept in sync deliberately:
# this script is an operator tool that talks to the database directly, so it must not
# invent transitions the API would refuse.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "uploaded": {"validating", "revoked"},
    "validating": {"validated", "uploaded", "revoked"},
    "validated": {"staging", "deprecated", "revoked"},
    "staging": {"production", "validated", "deprecated", "revoked"},
    "production": {"deprecated", "revoked"},
    "deprecated": {"staging", "revoked"},
    "revoked": {"validating"},
}

ORDER = ["revoked", "validating", "validated", "staging", "production"]

REASON_STANDARD = (
    "Legacy production model, in continuous service on the CSense platform since 2022. "
    "Promoted for use by the new runtime by owner direction."
)
REASON_BIOMETRIC = (
    "Biometric model (InsightFace) backing the staff-attendance feature, in continuous "
    "service on the legacy CSense platform since 2022. Owner has directed that all "
    "models be wired up; promotion acknowledged with biometric classification recorded. "
    "See CLARIFICATIONS.md #16."
)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "csense")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _path_to(current: str, target: str) -> list[str]:
    """Shortest forward walk through the state machine, validated hop by hop."""
    if current == target:
        return []
    try:
        start, end = ORDER.index(current), ORDER.index(target)
    except ValueError:
        raise SystemExit(f"Cannot route from '{current}' to '{target}'.") from None
    if end < start:
        raise SystemExit(f"This tool only promotes forward; '{current}' -> '{target}' is backwards.")

    steps = ORDER[start + 1 : end + 1]
    state = current
    for step in steps:
        if step not in VALID_TRANSITIONS[state]:
            raise SystemExit(f"Illegal transition {state} -> {step}")
        state = step
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="production", choices=ORDER[1:])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT mv.id, m.name, mv.state, mv.access_classification
            FROM model_versions mv
            JOIN models m ON m.id = mv.model_id
            WHERE mv.provenance->>'origin' = 'legacy_csense_migration'
            ORDER BY mv.access_classification, m.name
            """
        )
        versions = cur.fetchall()
        if not versions:
            print("No migrated legacy models found.", file=sys.stderr)
            raise SystemExit(1)

        promoted = unchanged = 0
        for version_id, name, state, classification in versions:
            steps = _path_to(state, args.target)
            if not steps:
                print(f"  {name:36s} already {state}")
                unchanged += 1
                continue

            flag = " [biometric]" if classification == "biometric" else ""
            print(f"  {name:36s} {state} -> {' -> '.join(steps)}{flag}")
            if args.dry_run:
                continue

            reason = REASON_BIOMETRIC if classification == "biometric" else REASON_STANDARD
            current = state
            for step in steps:
                cur.execute(
                    "UPDATE model_versions SET state = %s, state_reason = %s WHERE id = %s",
                    (step, reason, version_id),
                )
                cur.execute(
                    """
                    INSERT INTO audit_events
                        (tenant_id, actor_type, actor_id, action, target_type, target_id,
                         outcome, reason, before_patch, after_patch)
                    VALUES (NULL, 'operator', 'promote_legacy_models.py', 'model.promote',
                            'model_version', %s, 'success', %s, %s, %s)
                    """,
                    (
                        str(version_id),
                        reason,
                        psycopg.types.json.Json({"state": current}),
                        psycopg.types.json.Json({"state": step}),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO outbox_events
                        (tenant_id, aggregate_type, aggregate_id, event_type, payload)
                    VALUES (NULL, 'model_version', %s, 'model.version.state.changed.v1', %s)
                    """,
                    (
                        str(version_id),
                        psycopg.types.json.Json(
                            {
                                "model_version_id": str(version_id),
                                "model_name": name,
                                "previous_state": current,
                                "new_state": step,
                                "access_classification": classification,
                                "biometric_acknowledged": classification == "biometric",
                            }
                        ),
                    ),
                )
                current = step
            promoted += 1

        if args.dry_run:
            print("\nDry run; nothing written.")
            return
        conn.commit()

    print(f"\n{promoted} promoted to {args.target}, {unchanged} already there.")


if __name__ == "__main__":
    main()
