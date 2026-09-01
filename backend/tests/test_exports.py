"""CSV export serialization - pure, no infra needed (see
csense_shared/exports/incidents.py's own docstring for why it's split out this way)."""
from __future__ import annotations

import csv
import datetime as dt
import io

from csense_shared.exports import INCIDENT_CSV_COLUMNS, build_incidents_csv


def _rows_from_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_header_row_matches_the_declared_column_order():
    csv_bytes = build_incidents_csv([])
    text = csv_bytes.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header.split(",") == INCIDENT_CSV_COLUMNS


def test_a_row_round_trips_with_the_right_values():
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "incident_number": 42,
        "type_code": "loitering",
        "severity": "high",
        "status": "resolved",
        "title": "Loitering detected",
        "summary": None,
        "camera_id": "22222222-2222-2222-2222-222222222222",
        "site_id": "33333333-3333-3333-3333-333333333333",
        "detection_count": 3,
        "first_detected_at": dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.UTC),
        "last_detected_at": dt.datetime(2026, 8, 31, 10, 5, tzinfo=dt.UTC),
        "acknowledged_at": None,
        "resolution_code": "confirmed_true_positive",
        "resolution_summary": None,
    }
    parsed = _rows_from_csv(build_incidents_csv([row]))
    assert len(parsed) == 1
    out = parsed[0]
    assert out["incident_number"] == "42"
    assert out["title"] == "Loitering detected"
    assert out["first_detected_at"] == "2026-08-31T10:00:00+00:00"
    assert out["resolution_code"] == "confirmed_true_positive"


def test_none_values_become_empty_cells_not_the_string_none():
    row = {column: None for column in INCIDENT_CSV_COLUMNS}
    parsed = _rows_from_csv(build_incidents_csv([row]))
    assert all(value == "" for value in parsed[0].values())


def test_missing_keys_are_treated_as_none_not_a_crash():
    """A row dict missing a column entirely (e.g. a future column not yet selected by
    the caller's SQL) still produces a well-formed CSV rather than a KeyError."""
    parsed = _rows_from_csv(build_incidents_csv([{"id": "abc"}]))
    assert parsed[0]["id"] == "abc"
    assert parsed[0]["title"] == ""


def test_multiple_rows_preserve_order():
    rows = [{"id": "first"}, {"id": "second"}, {"id": "third"}]
    parsed = _rows_from_csv(build_incidents_csv(rows))
    assert [r["id"] for r in parsed] == ["first", "second", "third"]


def test_the_file_carries_a_utf8_bom_for_excel():
    csv_bytes = build_incidents_csv([])
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
