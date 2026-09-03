"""Manifest for models custom-trained in-house by Airivu interns, pulled 2026-09-03 from
16 personal proof-of-concept project bundles handed over for review.

**Only 3 of those 16 bundles contained a genuinely distinct, custom-trained model** -
found by hashing every `.pt` file, not by trusting a project's name or folder structure:

  - `Aggresive Behavior/yolov8n.pt` is **byte-identical** to the legacy `yolov8n-person`
    already in this registry - not re-registered.
  - `Aggresive Behavior/yolov8n-pose.pt` is a *different* checkpoint, but the project's own
    `child_safety_monitor.py` says outright it isn't a trained classifier: "Behavior
    classification... no training data needed... graduate to a trained classifier once you
    have labeled clips." It's a stock pose backbone driving a hand-written heuristic
    (punch/kick/push -> "fight"), not a custom-trained model - not registered as one.
  - `unattended_child/best.pt` and `unauthorized_adult_detection/best.pt` are
    **byte-identical** to each other - one model, reused across two different scripts, not
    two. Registered once.
  - Everything else in the 16 bundles was evidence video/images from personal test runs,
    a Windows venv full of vendored pip packages, or application code with no bundled
    weight file at all.

**License note the delivered files did not carry, worth recording rather than losing**:
`abuse_detection`'s own `data.yaml` (a Roboflow export inside the same zip) names its
training data as `suryas-workspace-lfjws/abuse-6sg5s-czea4` on Roboflow Universe, licensed
**CC BY 4.0** - a public third-party dataset, not solely in-house data. The trained weights
themselves are Airivu's, but CC BY requires attribution to the dataset wherever this
model's presence is disclosed, the same discipline already applied to FairFace's CC BY 4.0
terms in `uniface_model_manifest.py`. Recorded in `license_metadata` so this fact travels
with the artifact.

The other two (`classroom` hazard detector, `child`/`adult` detector) have no `data.yaml`
or equivalent in their zips - their training data's origin is undocumented. Marked
`review_required` for that reason alone, not because ownership is in question.

Like the legacy CSense estate and unlike the `uniface` pull, these were built by this
company's own team - but they have no production history with real customers either, so
`initial_state="uploaded"` (not `"validated"`), matching `uniface_model_manifest.py`'s
same reasoning: real validation before real reliance, not assumed from who wrote it.
"""
from __future__ import annotations

from legacy_model_manifest import LegacyModel

_INTERN_PROPRIETARY = {
    "license": "proprietary",
    "source": "Airivu intern-trained, in-house",
    "commercial_use": "owned_by_airivu",
    "review_required": True,
    "review_reason": "Training data origin not documented in the delivered project bundle.",
}
_INTERN_WITH_CC_BY_DATA = {
    "license": "proprietary (weights); training data CC BY 4.0",
    "source": "Airivu intern-trained, on a public Roboflow dataset",
    "commercial_use": "owned_by_airivu",
    "review_required": True,
    "attribution_required": (
        "Training data: suryas-workspace-lfjws/abuse-6sg5s-czea4 on Roboflow Universe "
        "(https://universe.roboflow.com/suryas-workspace-lfjws/abuse-6sg5s-czea4), CC BY 4.0. "
        "Attribute wherever this model's use is disclosed, not just in source code."
    ),
}


INTERN_MODELS: tuple[LegacyModel, ...] = (
    LegacyModel(
        local_name="abuse_best.pt",
        model_name="intern-abuse-detection",
        version_label="intern-2026-07",
        task_code="abuse_detection",
        description="Single-class 'Abuse' detector, YOLO architecture, trained in-house on a public Roboflow dataset.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="1afb1846b5063b9b68db52ae662616f8dd119ca5098b2aee3aaa9050191b5969",
        legacy_paths=("abuse_detection_folder/content/Abuse.v1i.yolov11/runs/detect/train-3/weights/best.pt",),
        legacy_classes=(),
        license_metadata=_INTERN_WITH_CC_BY_DATA,
        initial_state="uploaded",
        label_map={"0": "Abuse"},
        notes=(
            "The canonical training-run output (runs/detect/train-3/weights/best.pt), not "
            "the top-level abuse_detection.pt in the same zip, which is a different, "
            "unexplained checkpoint (different SHA-256) - possibly an earlier or manually "
            "copied version. best.pt was chosen as the real, standard Ultralytics "
            "convention for 'this training run's actual result'."
        ),
    ),
    LegacyModel(
        local_name="classroom_model.pt",
        model_name="intern-classroom-hazard-detection",
        version_label="intern-2026-07",
        task_code="classroom_hazard_detection",
        description="4-class classroom hazard detector: hazardous_object, wet_floor, fire, smoke.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="e0036b68cfb9f3a73acb17f5929fb87f03922b1b8de8602e2fbdfb9bae0d08a1",
        legacy_paths=("classroom_monitoting/classroom_monit/model.pt",),
        legacy_classes=(),
        license_metadata=_INTERN_PROPRIETARY,
        initial_state="uploaded",
        label_map={"0": "hazardous_object", "1": "wet_floor", "2": "fire", "3": "smoke"},
        notes="Class list read directly from the project's own main.py (CLASS_NAMES dict), not guessed from the filename.",
    ),
    LegacyModel(
        local_name="child_adult_best.pt",
        model_name="intern-child-adult-detection",
        version_label="intern-2026-07",
        task_code="child_adult_detection",
        description="2-class child/adult person detector, YOLO architecture.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="fba87f24abb35f52775ec3dcd9663655edf8812a2335f5f0441b47896ddaf6fd",
        legacy_paths=(
            "unattended_child/best.pt",
            "Protection/best.pt",  # unauthorized_adult_detection.zip - byte-identical file
        ),
        legacy_classes=(),
        license_metadata=_INTERN_PROPRIETARY,
        initial_state="uploaded",
        label_map={"0": "Adult", "1": "Child"},  # read directly from the checkpoint's own embedded model.names via ultralytics.YOLO(path).names - NOT inferred from either script's own variable-naming order, which turned out to disagree (this manifest's first draft had it backwards)
        notes=(
            "Byte-identical to unauthorized_adult_detection/Protection/best.pt - one "
            "model, reused by two different personal scripts (unattended-child dwell-time "
            "logic; unauthorized-adult face-match logic). Registered once. The face-match "
            "reference photo in that second project (a real named person's photo, used as "
            "an 'authorized person' enrollment) was deliberately NOT imported anywhere - "
            "that is per-site enrollment data, not a portable model, and carries its own "
            "consent question this registry has no business holding."
        ),
    ),
)
