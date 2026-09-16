"""CSV export serialization for detections - pure, no infra needed (see
csense_shared/exports/detections.py's own docstring). Mirrors test_exports.py's own
structure for the incidents export, one test file per export type."""
from __future__ import annotations

import csv
import datetime as dt
import io

from csense_shared.exports import DETECTION_CSV_COLUMNS, build_detections_csv, summarize_objects


def _rows_from_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_header_row_matches_the_declared_column_order():
    csv_bytes = build_detections_csv([])
    text = csv_bytes.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header.split(",") == DETECTION_CSV_COLUMNS


def test_a_row_round_trips_with_the_right_values():
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "event_type": "loitering",
        "source_event_id": "edge-abc-123",
        "confidence": 0.87,
        "captured_at": dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.UTC),
        "cloud_received_at": dt.datetime(2026, 8, 31, 10, 0, 5, tzinfo=dt.UTC),
        "camera_id": "22222222-2222-2222-2222-222222222222",
        "camera_name": "Front gate",
        "site_id": "33333333-3333-3333-3333-333333333333",
        "site_name": "Main warehouse",
        "zone_name": "Entrance",
        "object_count": 2,
        "objects_summary": "person:0.92; car:0.75",
        "incident_id": "44444444-4444-4444-4444-444444444444",
        "incident_number": 7,
    }
    parsed = _rows_from_csv(build_detections_csv([row]))
    assert len(parsed) == 1
    out = parsed[0]
    assert out["camera_name"] == "Front gate"
    assert out["captured_at"] == "2026-08-31T10:00:00+00:00"
    assert out["object_count"] == "2"
    assert out["objects_summary"] == "person:0.92; car:0.75"
    assert out["incident_number"] == "7"


def test_none_values_become_empty_cells_not_the_string_none():
    row = {column: None for column in DETECTION_CSV_COLUMNS}
    parsed = _rows_from_csv(build_detections_csv([row]))
    assert all(value == "" for value in parsed[0].values())


def test_missing_keys_are_treated_as_none_not_a_crash():
    parsed = _rows_from_csv(build_detections_csv([{"id": "abc"}]))
    assert parsed[0]["id"] == "abc"
    assert parsed[0]["camera_name"] == ""


def test_multiple_rows_preserve_order():
    rows = [{"id": "first"}, {"id": "second"}, {"id": "third"}]
    parsed = _rows_from_csv(build_detections_csv(rows))
    assert [r["id"] for r in parsed] == ["first", "second", "third"]


def test_the_file_carries_a_utf8_bom_for_excel():
    csv_bytes = build_detections_csv([])
    assert csv_bytes.startswith(b"\xef\xbb\xbf")


def test_summarize_objects_empty_or_none_is_empty_string():
    assert summarize_objects(None) == ""
    assert summarize_objects([]) == ""


def test_summarize_objects_joins_class_and_confidence():
    objects = [
        {"class": "person", "confidence": 0.923},
        {"class_name": "car", "confidence": 0.75},
    ]
    assert summarize_objects(objects) == "person:0.92; car:0.75"


def test_summarize_objects_handles_a_missing_confidence_or_class():
    # A malformed/older object row shouldn't crash the whole export - matches this
    # project's own "no guessing, but also no silent 500 on one odd row" discipline.
    assert summarize_objects([{"confidence": 0.5}]) == "unknown:0.50"
    assert summarize_objects([{"class": "person"}]) == "person"
