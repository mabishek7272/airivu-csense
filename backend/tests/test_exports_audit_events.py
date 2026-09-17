"""CSV export serialization for audit events - pure, no infra needed (see
csense_shared/exports/audit_events.py's own docstring). Mirrors test_exports.py's own
structure, one test file per export type."""
from __future__ import annotations

import csv
import datetime as dt
import io

from csense_shared.exports import AUDIT_EVENT_CSV_COLUMNS, build_audit_events_csv


def _rows_from_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_header_row_matches_the_declared_column_order():
    csv_bytes = build_audit_events_csv([])
    text = csv_bytes.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header.split(",") == AUDIT_EVENT_CSV_COLUMNS


def test_a_row_round_trips_with_the_right_values():
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "occurred_at": dt.datetime(2026, 8, 31, 10, 0, tzinfo=dt.UTC),
        "actor_type": "user",
        "actor_id": "22222222-2222-2222-2222-222222222222",
        "actor_display_name": "Abelamm",
        "action": "camera.manage",
        "target_type": "camera",
        "target_id": "33333333-3333-3333-3333-333333333333",
        "outcome": "success",
        "reason": None,
    }
    parsed = _rows_from_csv(build_audit_events_csv([row]))
    assert len(parsed) == 1
    out = parsed[0]
    assert out["actor_display_name"] == "Abelamm"
    assert out["occurred_at"] == "2026-08-31T10:00:00+00:00"
    assert out["action"] == "camera.manage"
    assert out["reason"] == ""


def test_a_pipeline_actor_has_no_display_name_and_that_is_correct():
    """A `pipeline` actor is a system actor, not a real account - `actor_display_name`
    being empty for it is the correct, expected shape, not a join failure (see
    app.api.audit's own module docstring)."""
    row = {
        "id": "abc", "actor_type": "pipeline", "actor_id": "zone.intrusion",
        "actor_display_name": None, "action": "incident.created",
    }
    parsed = _rows_from_csv(build_audit_events_csv([row]))
    assert parsed[0]["actor_display_name"] == ""


def test_none_values_become_empty_cells_not_the_string_none():
    row = {column: None for column in AUDIT_EVENT_CSV_COLUMNS}
    parsed = _rows_from_csv(build_audit_events_csv([row]))
    assert all(value == "" for value in parsed[0].values())


def test_missing_keys_are_treated_as_none_not_a_crash():
    parsed = _rows_from_csv(build_audit_events_csv([{"id": "abc"}]))
    assert parsed[0]["id"] == "abc"
    assert parsed[0]["action"] == ""


def test_multiple_rows_preserve_order():
    rows = [{"id": "first"}, {"id": "second"}, {"id": "third"}]
    parsed = _rows_from_csv(build_audit_events_csv(rows))
    assert [r["id"] for r in parsed] == ["first", "second", "third"]


def test_the_file_carries_a_utf8_bom_for_excel():
    csv_bytes = build_audit_events_csv([])
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
