"""CSV serialization for an audit-events export job - the second of the two exports
CHECKLIST.md's own "Async reports/exports" entry named as remaining ("audit events and
camera health history exports remain unbuilt"). Same mechanism, same reasoning, as
`incidents.py`/`detections.py` (sibling modules) - kept pure and dependency-free so the
one part of the pipeline with real formatting logic stays unit-testable without a running
stack.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

# Column order is the contract - `build_audit_events_csv` and the API's own SELECT
# (exports.py) must agree on this order, same discipline every sibling export module
# already established. `actor_display_name` is included (not just actor_type/actor_id):
# an export is read by a human after the fact, exactly the case the friendly-name join
# (backend/tenant_api/app/api/audit.py's own docstring) was built for.
AUDIT_EVENT_CSV_COLUMNS = [
    "id",
    "occurred_at",
    "actor_type",
    "actor_id",
    "actor_display_name",
    "action",
    "target_type",
    "target_id",
    "outcome",
    "reason",
]


def _cell(value: Any) -> str:
    """Mirrors every sibling export module's own `_cell` exactly - `None` becomes an
    empty cell, a datetime becomes real ISO-8601, not `str(datetime)`'s space-separated
    form."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


def build_audit_events_csv(rows: list[dict[str, Any]]) -> bytes:
    """`rows` are dicts keyed by `AUDIT_EVENT_CSV_COLUMNS`, in any order - one row per
    audit event. Returns UTF-8 bytes with a BOM (`utf-8-sig`), same Excel-compatibility
    reasoning as every sibling export."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(AUDIT_EVENT_CSV_COLUMNS)
    for row in rows:
        writer.writerow(_cell(row.get(column)) for column in AUDIT_EVENT_CSV_COLUMNS)
    return buffer.getvalue().encode("utf-8-sig")
