"""Manifest describing every model imported from the legacy CSense platform.

Metadata is derived from the legacy detection classes in
`/var/www/csense/backend/detection_models_fixed.py` (ANPRModel, WorkerTimeModel,
FallDetectionModel, FireDetectionModel, CrowdDetectionModel, ZoneIntelligenceModel,
KitchenSafetyModel, StaffAttendanceModel, PersonalVehicleModel) and from where each
artifact sat in the legacy tree.

Two things this manifest deliberately records:

1. **Digest, not filename, is identity.** The legacy tree had 22 model paths but only 14
   distinct blobs, and two *different* files were both named `yolov8n.pt`. Filenames are
   therefore treated as untrusted hints; `sha256` is the identity.

2. **Licence and classification are explicit.** Ultralytics YOLOv8 weights are AGPL-3.0
   unless a commercial licence is held, which affects how a hosted service may use them;
   the InsightFace models perform biometric identification, which the PRD lists as a
   release-one non-goal. Both facts travel with the artifact instead of living in
   someone's memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LegacyModel:
    local_name: str
    model_name: str
    version_label: str
    task_code: str
    description: str
    framework: str
    runtime: str
    sha256: str
    legacy_paths: tuple[str, ...]
    legacy_classes: tuple[str, ...]
    license_metadata: dict
    access_classification: str = "standard"
    # Imported state. Operational models land as `validated` (they ran in production on
    # the legacy platform, but have not yet passed THIS platform's validation suite, so
    # they are explicitly not `production`). Biometric models land as `revoked`.
    initial_state: str = "validated"
    state_reason: str | None = None
    label_map: dict | None = None
    notes: str = ""
    hardware_profile: dict = field(default_factory=dict)


_AGPL = {
    "license": "AGPL-3.0",
    "source": "Ultralytics YOLOv8",
    "commercial_use": "requires_ultralytics_commercial_license",
    "review_required": True,
}
_CUSTOM = {
    "license": "proprietary-unverified",
    "source": "legacy CSense custom-trained weights",
    "commercial_use": "assumed_owned_by_airivu",
    "review_required": True,
}
_MIT_ISH = {
    "license": "see-upstream",
    "source": "open-image-models / fast-plate-ocr",
    "commercial_use": "verify_upstream_terms",
    "review_required": True,
}
_INSIGHTFACE = {
    "license": "InsightFace non-commercial research terms",
    "source": "InsightFace buffalo_l",
    "commercial_use": "prohibited_without_separate_license",
    "review_required": True,
}

LEGACY_MODELS: tuple[LegacyModel, ...] = (
    LegacyModel(
        local_name="yolov8n-base.pt",
        model_name="yolov8n-general",
        version_label="legacy-2026-08",
        task_code="object_detection",
        description="General-purpose YOLOv8-nano object detector used as the default backbone.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="31e20dde3def09e2cf938c7be6fe23d9150bbbe503982af13345706515f2ef95",
        legacy_paths=("/var/www/csense/backend/yolov8n.pt",),
        legacy_classes=("DetectionModelManager",),
        license_metadata=_AGPL,
        notes="Distinct bytes from the other legacy yolov8n.pt despite the identical filename.",
    ),
    LegacyModel(
        local_name="yolov8n-falldet.pt",
        model_name="yolov8n-person",
        version_label="legacy-2026-08",
        task_code="object_detection",
        description="YOLOv8-nano person detector shared by fall detection, crowd, and restricted-zone use cases.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36",
        legacy_paths=(
            "/var/www/csense/backend/Client-Fall-Detection/yolov8n.pt",
            "/var/www/csense/backend/models/crowd/yolov8n.pt",
            "/var/www/csense/backend/restricted_area/yolov8n.pt",
        ),
        legacy_classes=("FallDetectionModel", "CrowdDetectionModel", "ZoneIntelligenceModel"),
        license_metadata=_AGPL,
        notes="Same bytes deployed under three different paths in the legacy tree.",
    ),
    LegacyModel(
        local_name="yolov8n-pose.pt",
        model_name="yolov8n-pose",
        version_label="legacy-2026-08",
        task_code="pose_estimation",
        description="YOLOv8-nano pose estimation, used for posture and fall analysis on lighter hardware.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="7f80660bc2f97d664d86fc9f50fd5903af392fe332c0d603fa0dd6c78bf8844c",
        legacy_paths=("/var/www/csense/backend/yolov8n-pose.pt",),
        legacy_classes=("FallDetectionModel",),
        license_metadata=_AGPL,
    ),
    LegacyModel(
        local_name="yolov8m-pose.pt",
        model_name="yolov8m-pose",
        version_label="legacy-2026-08",
        task_code="pose_estimation",
        description="YOLOv8-medium pose estimation; primary keypoint model behind fall detection.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="dbe539ea268db2534390942cfdf206e521f376f19e5415967a57f6a2ddfa3c90",
        legacy_paths=(
            "/var/www/csense/backend/yolov8m-pose.pt",
            "/var/www/csense/backend/Client-Fall-Detection/yolov8m-pose.pt",
        ),
        legacy_classes=("FallDetectionModel",),
        license_metadata=_AGPL,
    ),
    LegacyModel(
        local_name="optimized150-fire.pt",
        model_name="fire-smoke-optimized150",
        version_label="legacy-2026-08",
        task_code="fire_smoke_detection",
        description="Custom-trained fire and smoke detector (legacy 'optimized150' weights).",
        framework="pytorch",
        runtime="ultralytics",
        sha256="cbe5840f952adab362689edc00197a85ec7543841c9db5d91d3a09ace01c5e64",
        legacy_paths=(
            "/var/www/csense/backend/optimized150.pt",
            "/var/www/csense/backend/Client-fire-and-smoke/optimized150.pt",
        ),
        legacy_classes=("FireDetectionModel",),
        license_metadata=_CUSTOM,
        label_map={"0": "fire", "1": "smoke"},
        notes="Label map inferred from legacy usage; confirm against training data before production promotion.",
    ),
    LegacyModel(
        local_name="yolov8m-fire.pt",
        model_name="fire-smoke-yolov8m",
        version_label="legacy-2026-08",
        task_code="fire_smoke_detection",
        description="YOLOv8-medium variant used alongside optimized150 for fire and smoke.",
        framework="pytorch",
        runtime="ultralytics",
        sha256="5d4a90cdc7a21786cc59cd19778e9eafff836df9e2da32524737c7ee6efe4fe5",
        legacy_paths=("/var/www/csense/backend/Client-fire-and-smoke/yolov8m.pt",),
        legacy_classes=("FireDetectionModel",),
        license_metadata=_AGPL,
    ),
    LegacyModel(
        local_name="bestY8_float32-kitchen.tflite",
        model_name="kitchen-safety-y8",
        version_label="legacy-2026-08",
        task_code="ppe_kitchen_safety",
        description="Kitchen safety / PPE compliance detector, float32 TFLite export.",
        framework="tflite",
        runtime="tflite",
        sha256="4a8c4b01ac1898f0aa886547e98d209ad0b26e5480cf8ab789c3b44a22f04597",
        legacy_paths=("/var/www/csense/backend/Client-Kitchen-Safety/bestY8_float32.tflite",),
        legacy_classes=("KitchenSafetyModel",),
        license_metadata=_CUSTOM,
        notes="Only TFLite artifact in the estate; the runtime must support a TFLite execution path.",
    ),
    LegacyModel(
        local_name="yolo-v9-t-384-license-plates-end2end.onnx",
        model_name="license-plate-detector",
        version_label="legacy-2026-08",
        task_code="license_plate_detection",
        description="YOLOv9-tiny 384px end-to-end license plate detector (ANPR stage 1).",
        framework="onnx",
        runtime="onnxruntime",
        sha256="888397b96d761c89db40bc9c305838e8652660f5e282c2cadebbe8d2951a77a8",
        legacy_paths=(
            "/var/www/csense/backend/yolo-v9-t-384-license-plates-end2end.onnx",
            "/var/www/csense/backend/Client-ANPR/yolo-v9-t-384-license-plates-end2end.onnx",
            "/var/www/csense/backend/models/worker_time/yolo-v9-t-384-license-plates-end2end.onnx",
        ),
        legacy_classes=("ANPRModel", "WorkerTimeModel", "PersonalVehicleModel"),
        license_metadata=_MIT_ISH,
    ),
    LegacyModel(
        local_name="cct_xs_v1_global.onnx",
        model_name="license-plate-ocr",
        version_label="legacy-2026-08",
        task_code="license_plate_ocr",
        description="CCT-XS global plate OCR model (ANPR stage 2, reads the cropped plate).",
        framework="onnx",
        runtime="onnxruntime",
        sha256="c1ae2ae031b10f8ab7ed6e3cdaf8d9c753477e299dbad3579c8f705a7d882134",
        legacy_paths=(
            "/var/www/csense/backend/cct_xs_v1_global.onnx",
            "/var/www/csense/backend/Client-ANPR/cct_xs_v1_global.onnx",
            "/var/www/csense/backend/models/worker_time/cct_xs_v1_global.onnx",
        ),
        legacy_classes=("ANPRModel", "WorkerTimeModel", "PersonalVehicleModel"),
        license_metadata=_MIT_ISH,
        notes="ANPR is a two-stage pipeline: this pairs with license-plate-detector.",
    ),
    # --- Biometric set -----------------------------------------------------------
    # Imported and preserved at the owner's request, but registered `revoked` so they
    # cannot be assigned to a pipeline without an explicit, audited promotion.
    LegacyModel(
        local_name="buffalo_l-det_10g.onnx",
        model_name="insightface-buffalo-l-detect",
        version_label="legacy-2026-08",
        task_code="face_detection",
        description="InsightFace buffalo_l face detector (SCRFD 10G).",
        framework="onnx",
        runtime="onnxruntime",
        sha256="5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
        legacy_paths=("/var/www/.insightface/models/buffalo_l/det_10g.onnx",),
        legacy_classes=("StaffAttendanceModel",),
        license_metadata=_INSIGHTFACE,
        access_classification="biometric",
        initial_state="revoked",
        state_reason=(
            "Biometric model. Facial recognition is a release-one non-goal in the PRD and "
            "the deployment jurisdiction is not yet confirmed. Preserved for future use; "
            "requires explicit promotion plus privacy/legal sign-off before any pipeline "
            "may reference it."
        ),
    ),
    LegacyModel(
        local_name="buffalo_l-w600k_r50.onnx",
        model_name="insightface-buffalo-l-recognition",
        version_label="legacy-2026-08",
        task_code="face_recognition",
        description="InsightFace buffalo_l ArcFace R50 recognition model producing 512-d face embeddings.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
        legacy_paths=("/var/www/.insightface/models/buffalo_l/w600k_r50.onnx",),
        legacy_classes=("StaffAttendanceModel",),
        license_metadata=_INSIGHTFACE,
        access_classification="biometric",
        initial_state="revoked",
        state_reason=(
            "Generates biometric templates (face embeddings) used to identify individuals. "
            "Highest-sensitivity artifact in the estate; requires explicit promotion plus "
            "privacy/legal sign-off."
        ),
    ),
    LegacyModel(
        local_name="buffalo_l-1k3d68.onnx",
        model_name="insightface-buffalo-l-landmark-3d",
        version_label="legacy-2026-08",
        task_code="face_landmark",
        description="InsightFace buffalo_l 3D 68-point facial landmark model.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc",
        legacy_paths=("/var/www/.insightface/models/buffalo_l/1k3d68.onnx",),
        legacy_classes=("StaffAttendanceModel",),
        license_metadata=_INSIGHTFACE,
        access_classification="biometric",
        initial_state="revoked",
        state_reason="Part of the biometric face-analysis set; see recognition model.",
    ),
    LegacyModel(
        local_name="buffalo_l-2d106det.onnx",
        model_name="insightface-buffalo-l-landmark-2d",
        version_label="legacy-2026-08",
        task_code="face_landmark",
        description="InsightFace buffalo_l 2D 106-point facial landmark model.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf",
        legacy_paths=("/var/www/.insightface/models/buffalo_l/2d106det.onnx",),
        legacy_classes=("StaffAttendanceModel",),
        license_metadata=_INSIGHTFACE,
        access_classification="biometric",
        initial_state="revoked",
        state_reason="Part of the biometric face-analysis set; see recognition model.",
    ),
    LegacyModel(
        local_name="buffalo_l-genderage.onnx",
        model_name="insightface-buffalo-l-genderage",
        version_label="legacy-2026-08",
        task_code="face_attribute",
        description="InsightFace buffalo_l gender and age estimation model.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb",
        legacy_paths=("/var/www/.insightface/models/buffalo_l/genderage.onnx",),
        legacy_classes=("StaffAttendanceModel",),
        license_metadata=_INSIGHTFACE,
        access_classification="biometric",
        initial_state="revoked",
        state_reason=(
            "Infers protected demographic attributes from faces. Beyond the PRD scope and "
            "subject to additional restrictions in several jurisdictions."
        ),
    ),
)


def by_local_name() -> dict[str, LegacyModel]:
    return {m.local_name: m for m in LEGACY_MODELS}
