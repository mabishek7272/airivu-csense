"""CSV serialization for an incidents export job (CHECKLIST: "Async reports/exports with
time-limited download").

Kept pure and dependency-free on purpose - no DB, no MinIO - so the one part of the
export pipeline with real formatting logic (column order, timestamp format, `None`
handling) is unit-testable without a running stack, the same reasoning
`cameras/health.py` gives for staying dependency-free.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

# Column order is the contract - `build_incidents_csv` and the API's own SELECT
# (exports.py) must agree on this order; a mismatch would silently mislabel columns
# rather than error, so both sides import this list instead of each hard-coding it.
INCIDENT_CSV_COLUMNS = [
    "id",
    "incident_number",
    "type_code",
    "severity",
    "status",
    "title",
    "summary",
    "camera_id",
    "site_id",
    "detection_count",
    "first_detected_at",
    "last_detected_at",
    "acknowledged_at",
    "resolution_code",
    "resolution_summary",
]


def _cell(value: Any) -> str:
    """`None` becomes an empty cell, not the literal string "None"; a datetime becomes a
    real ISO-8601 string a spreadsheet can sort/filter on, not `str(datetime)`'s
    space-separated form."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


def build_incidents_csv(rows: list[dict[str, Any]]) -> bytes:
    """`rows` are dicts keyed by `INCIDENT_CSV_COLUMNS`, in any order - one row per
    incident. Returns UTF-8 bytes with a BOM (`utf-8-sig`): Excel, the tool this file is
    most often actually opened in, otherwise mis-detects a BOM-less UTF-8 CSV as the
    system codepage and mangles anything outside ASCII."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(INCIDENT_CSV_COLUMNS)
    for row in rows:
        writer.writerow(_cell(row.get(column)) for column in INCIDENT_CSV_COLUMNS)
    return buffer.getvalue().encode("utf-8-sig")
