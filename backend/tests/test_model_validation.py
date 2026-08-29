"""The pure metrics arithmetic behind the golden-dataset benchmark harness.

`scripts/run_model_validation.py` itself calls real inference and a real API - not
something this suite runs (see that script's own docstring; `scripts/e2e_model_validation
_gate.py` is where the real end-to-end behaviour, including the promotion gate, is
verified against the real running stack). What's pinned here is `compute_metrics`, the one
piece of that script that's pure logic: recall/false-positive-rate/latency arithmetic and
the pass/fail threshold decision.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_runner():
    """Loads by path, not by adding `scripts/` to `sys.path` and importing by name -
    same reasoning as `test_nvr_adapter.py`'s own `_load_nvr_adapter`: nothing in this
    repo's own scripts/ directory collides today, but a module named generically enough
    to import cleanly is worth the same discipline regardless."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_model_validation.py"
    spec = importlib.util.spec_from_file_location("csense_run_model_validation", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_runner = _load_runner()
compute_metrics = _runner.compute_metrics


def _image(has_plate: bool, detected: bool, ms: float = 100.0) -> dict:
    return {"has_plate": has_plate, "detected": detected, "inference_ms": ms}


def test_perfect_recall_and_no_false_positives_passes():
    per_image = [
        _image(True, True), _image(True, True), _image(False, False), _image(False, False),
    ]
    metrics, _, status = compute_metrics(per_image, min_recall=0.8, max_false_positive_rate=0.5)

    assert metrics["recall"] == 1.0
    assert metrics["false_positive_rate"] == 0.0
    assert status == "passed"


def test_a_missed_positive_lowers_recall_and_can_fail_the_run():
    per_image = [
        _image(True, True), _image(True, False), _image(True, False), _image(True, False),
    ]
    metrics, _, status = compute_metrics(per_image, min_recall=0.8, max_false_positive_rate=0.5)

    assert metrics["recall"] == 0.25
    assert status == "failed"


def test_recall_exactly_at_the_threshold_passes_not_fails():
    """The threshold is inclusive - `>=`, matching how the promotion gate and every
    other threshold comparison in this codebase treats a boundary value."""
    per_image = [_image(True, True), _image(True, True), _image(True, True), _image(True, False)]
    metrics, _, status = compute_metrics(per_image, min_recall=0.75, max_false_positive_rate=1.0)

    assert metrics["recall"] == 0.75
    assert status == "passed"


def test_false_positive_rate_over_threshold_fails_even_with_perfect_recall():
    per_image = [_image(True, True), _image(False, True), _image(False, True)]
    metrics, _, status = compute_metrics(per_image, min_recall=1.0, max_false_positive_rate=0.4)

    assert metrics["recall"] == 1.0
    assert metrics["false_positive_rate"] == 1.0
    assert status == "failed"


def test_no_positive_examples_reports_recall_as_none_not_zero_or_one():
    """A dataset with no positives to measure recall against must not silently claim
    perfect or zero recall - either would misrepresent what was actually checked."""
    per_image = [_image(False, False), _image(False, True)]
    metrics, _, status = compute_metrics(per_image, min_recall=0.8, max_false_positive_rate=1.0)

    assert metrics["recall"] is None
    # No recall requirement to fail on, and the fpr threshold (1.0) is satisfied.
    assert status == "passed"


def test_no_negative_examples_reports_false_positive_rate_as_none():
    per_image = [_image(True, True), _image(True, True)]
    metrics, _, status = compute_metrics(per_image, min_recall=1.0, max_false_positive_rate=0.0)

    assert metrics["false_positive_rate"] is None
    assert status == "passed"


def test_latency_summary_is_computed_from_real_per_image_timings():
    per_image = [_image(True, True, ms=m) for m in (50.0, 100.0, 150.0, 200.0)]
    metrics, _, _ = compute_metrics(per_image, min_recall=1.0, max_false_positive_rate=1.0)

    assert metrics["mean_inference_ms"] == 125.0
    assert metrics["p95_inference_ms"] in (150.0, 200.0)  # small-N p95 is index-based, not interpolated


def test_thresholds_are_echoed_back_verbatim_not_hardcoded():
    _, thresholds, _ = compute_metrics([_image(True, True)], min_recall=0.42, max_false_positive_rate=0.99)
    assert thresholds == {"min_recall": 0.42, "max_false_positive_rate": 0.99}
