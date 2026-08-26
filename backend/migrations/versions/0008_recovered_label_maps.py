"""Records label maps recovered from the legacy system.

Ultralytics `.pt` checkpoints carry their own class names, so those models were already
correct. Two families were not: the kitchen-safety TFLite export and the licence-plate
ONNX models have no embedded names, so the runtime was returning numeric class ids. Since
incident types are derived from class names, those detections could not have driven a
meaningful alert.

Recovered from the legacy host:
  - `Client-Kitchen-Safety/classes.txt` and `KitchenSafetyModel.CLASSES` in
    detection_models_fixed.py, which agree exactly (6 classes).
  - The licence-plate detector is single-class by construction (plate / not-plate).

Also recorded here: which kitchen classes represent a *violation*. The legacy
`KitchenSafetyModel.VIOLATION_CLASSES` treats only 3 of the 6 as alertable - `glove`,
`hairnet` and `maskon` are compliant states and must not raise an incident. Losing that
distinction would turn every correctly-dressed worker into an alert, so it is captured on
the model version rather than left to be rediscovered later.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-26
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

KITCHEN_LABELS = {
    "0": "glove",
    "1": "hairnet",
    "2": "maskoff",
    "3": "maskon",
    "4": "no_glove",
    "5": "no_hairnet",
}

# Only these three are violations; the rest are compliant states (legacy
# KitchenSafetyModel.VIOLATION_CLASSES).
KITCHEN_VIOLATIONS = ["maskoff", "no_glove", "no_hairnet"]

PLATE_LABELS = {"0": "license_plate"}


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            UPDATE model_versions mv
            SET label_map = CAST(:labels AS jsonb),
                output_schema = CAST(:schema AS jsonb)
            FROM models m
            WHERE m.id = mv.model_id AND m.name = 'kitchen-safety-y8'
            """
        ),
        {
            "labels": json.dumps(KITCHEN_LABELS),
            "schema": json.dumps(
                {
                    "layout": "raw_yolo_head",
                    "shape": "(1, 4 + num_classes, num_anchors)",
                    "num_classes": 6,
                    "requires_nms": True,
                    "violation_classes": KITCHEN_VIOLATIONS,
                    "source": "legacy Client-Kitchen-Safety/classes.txt",
                }
            ),
        },
    )

    bind.execute(
        sa.text(
            """
            UPDATE model_versions mv
            SET label_map = CAST(:labels AS jsonb),
                output_schema = CAST(:schema AS jsonb)
            FROM models m
            WHERE m.id = mv.model_id AND m.name = 'license-plate-detector'
            """
        ),
        {
            "labels": json.dumps(PLATE_LABELS),
            "schema": json.dumps(
                {
                    "layout": "yolo_end2end",
                    "columns": ["batch_index", "x1", "y1", "x2", "y2", "score", "class"],
                    "num_classes": 1,
                    "requires_nms": False,
                }
            ),
        },
    )

    # The OCR model does not emit detections at all - it returns a character sequence for
    # an already-cropped plate. Recording that stops the runtime from being asked to
    # decode it as a detector and stops the next person assuming it is broken.
    bind.execute(
        sa.text(
            """
            UPDATE model_versions mv
            SET output_schema = CAST(:schema AS jsonb)
            FROM models m
            WHERE m.id = mv.model_id AND m.name = 'license-plate-ocr'
            """
        ),
        {
            "schema": json.dumps(
                {
                    "layout": "character_sequence",
                    "detections": False,
                    "note": (
                        "Stage 2 of ANPR. Consumes a plate crop produced by "
                        "license-plate-detector and returns characters, not boxes. Call via "
                        "the runtime's raw_infer path from the ANPR pipeline stage."
                    ),
                }
            )
        },
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE model_versions mv SET label_map = NULL, output_schema = NULL
        FROM models m
        WHERE m.id = mv.model_id
          AND m.name IN ('kitchen-safety-y8', 'license-plate-detector', 'license-plate-ocr')
        """
    )
