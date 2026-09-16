"""CSV serialization for a detections export job - the "easy next slice" CHECKLIST.md's
own "Async reports/exports" entry named: the same `export_jobs`/background-task/
presigned-download mechanism `incidents.py` (sibling module) already established, just a
new `export_type` and its own query-building function. Same "pure, dependency-free"
reasoning as that module's own docstring - kept unit-testable without a running stack.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

# Column order is the contract - `build_detections_csv` and the API's own SELECT
# (exports.py) must agree on this order, same discipline `incidents.py`'s own
# `INCIDENT_CSV_COLUMNS` already established.
DETECTION_CSV_COLUMNS = [
    "id",
    "event_type",
    "source_event_id",
    "confidence",
    "captured_at",
    "cloud_received_at",
    "camera_id",
    "camera_name",
    "site_id",
    "site_name",
    "zone_name",
    "object_count",
    # `objects` itself is a nested JSON list (class/confidence/bbox/track_id per detected
    # object) - a CSV cell can't hold that structure, so it's flattened to a
    # semicolon-joined "class:confidence" summary rather than dropped entirely. The full
    # structured data is still reachable per-detection via `GET /api/v1/tenant/
    # detections/{id}` - this is a spreadsheet-friendly summary, not the only copy.
    "objects_summary",
    "incident_id",
    "incident_number",
]


def _cell(value: Any) -> str:
    """`None` becomes an empty cell, not the literal string "None"; a datetime becomes a
    real ISO-8601 string a spreadsheet can sort/filter on, not `str(datetime)`'s
    space-separated form. Mirrors `incidents.py`'s own `_cell` exactly (kept as a separate
    copy, not a shared import, so each export type's formatting rules can diverge later
    without one module reaching into the other)."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


def summarize_objects(objects: list[dict[str, Any]] | None) -> str:
    """`[{"class": "person", "confidence": 0.92}, ...]` -> `"person:0.92; car:0.75"`.
    Exported separately from `build_detections_csv` so the caller can build the row dict
    directly from the same JSONB shape `detections.py`'s own `_row_to_detection` already
    parses, rather than needing a second, parallel object-shape assumption."""
    if not objects:
        return ""
    parts = []
    for obj in objects:
        class_name = obj.get("class") or obj.get("class_name") or "unknown"
        confidence = obj.get("confidence")
        parts.append(f"{class_name}:{confidence:.2f}" if confidence is not None else class_name)
    return "; ".join(parts)


def build_detections_csv(rows: list[dict[str, Any]]) -> bytes:
    """`rows` are dicts keyed by `DETECTION_CSV_COLUMNS`, in any order - one row per
    detection. Returns UTF-8 bytes with a BOM (`utf-8-sig`), same Excel-compatibility
    reasoning as `build_incidents_csv`."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(DETECTION_CSV_COLUMNS)
    for row in rows:
        writer.writerow(_cell(row.get(column)) for column in DETECTION_CSV_COLUMNS)
    return buffer.getvalue().encode("utf-8-sig")
