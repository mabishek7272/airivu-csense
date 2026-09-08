"""Manifest for the clean-licensed subset of the `uniface` model zoo
(https://github.com/yakhyo/uniface), pulled 2026-09-03.

**Why only 15 of the 62 weight entries in `uniface`'s own `MODEL_REGISTRY`, and why one
variant per family rather than all of them.** `uniface` ships one MIT-licensed Python
wrapper around weights sourced from ~15 separate upstream projects, each carrying its own
license - the wrapper's own README says as much ("some pretrained weights are not [MIT];
check licenses before shipping commercially") and points at a dedicated attribution page
rather than listing terms inline. That page was read in full and cross-checked against
`uniface`'s actual `MODEL_REGISTRY` dict (not just the README's summary):

  - **YOLOv5-Face, YOLOv8-Face are GPL-3.0** - real copyleft, the same class of constraint
    as the AGPL question already recorded for the legacy YOLOv8 models. Excluded.

  - **SCRFD, ArcFace, AgeGenderWeights, LandmarkWeights are sourced from InsightFace**, and
    `uniface`'s own attribution table labels them "MIT" - which is wrong, or at best
    dangerously imprecise. Checked by hash, not by trusting the label: `SCRFDWeights.
    SCRFD_10G_KPS`, `ArcFaceWeights.RESNET`, `AgeGenderWeights.DEFAULT`, and
    `LandmarkWeights.DEFAULT` are **byte-for-byte identical** (same SHA-256) to
    `buffalo_l-det_10g.onnx`, `buffalo_l-w600k_r50.onnx`, `buffalo_l-genderage.onnx`, and
    `buffalo_l-2d106det.onnx` respectively - the exact InsightFace files already sitting in
    this registry `state='revoked'` (`legacy_model_manifest.py`) because InsightFace's own
    model zoo states "ALL models are available for non-commercial research purposes only."
    A full sweep of every hash in `uniface`'s `MODEL_REGISTRY` against all five known
    buffalo_l hashes found exactly these four matches and no others. `ArcFaceWeights.MNET`
    and `SCRFDWeights.SCRFD_500M_KPS` are not proven byte-identical to an already-known
    file, but are still explicitly InsightFace-sourced per `uniface`'s own class
    docstrings, so excluded on the same non-commercial-by-default basis. All four families
    excluded entirely - not imported, not quarantined here, simply not pulled.

  - **HeadPoseWeights, EDifFIQAWeights, XSegWeights are not in `uniface`'s own license
    table at all** (silence, not a claim of MIT). `XSegWeights` additionally sources from
    `iperov/DeepFaceLab`, a deepfake-generation toolkit - excluded regardless of license
    for that reason alone. All three excluded.

  - **The remaining 15 families are genuinely clean** (MIT / Apache-2.0 / BSD-3-Clause /
    CC BY 4.0, each confirmed against `uniface`'s own attribution page, not assumed from
    provenance) and are what this manifest imports - one representative variant per family
    (the smallest/most practical, e.g. `RetinaFaceWeights.MNET_V2` rather than all seven
    RetinaFace sizes), not the full zoo. Widening to more variants later is additive.

**Unlike the legacy CSense estate, these models have zero production history with this
business** - they are freshly-obtained community weights, never before deployed here. So
unlike `legacy_model_manifest.py`'s deliberate choice of `initial_state="validated"`
(true for the legacy models: they ran in real production for years, just not through
*this* platform's own validation gate), every entry here lands as `initial_state=
"uploaded"` - the honest starting state, requiring a real validation run
(`POST .../validation-runs`, then `promote` through `validating` -> `validated`) before
any of them can reach `staging`/`production`. None are promoted by the import script.

FairFace is CC BY 4.0, which requires attribution wherever its output is surfaced (not
just in code) - recorded in this manifest's own metadata so that requirement travels with
the artifact rather than living in someone's memory, matching this file's own stated
discipline for licence facts.
"""
from __future__ import annotations

from legacy_model_manifest import LegacyModel

_MIT = {"license": "MIT", "source": "yakhyo/uniface and sibling repos", "commercial_use": "permitted", "review_required": False}
_APACHE = {"license": "Apache-2.0", "source": "yakhyo/uniface (weights from Google MediaPipe / re-exports)", "commercial_use": "permitted", "review_required": False}
_BSD = {"license": "BSD-3-Clause", "source": "yakhyo/face-attribute (weights (c) Qualcomm Technologies, Inc.)", "commercial_use": "permitted", "review_required": False}
_CC_BY = {
    "license": "CC BY 4.0",
    "source": "yakhyo/fairface-onnx",
    "commercial_use": "permitted_with_attribution",
    "review_required": True,
    "attribution_required": "Wherever FairFace output is surfaced to a user, not just in source code.",
}


def _provenance(url: str, uniface_class: str, uniface_member: str) -> dict:
    return {
        "origin": "uniface_model_zoo",
        "source_url": url,
        "uniface_repo": "https://github.com/yakhyo/uniface",
        "uniface_enum": f"{uniface_class}.{uniface_member}",
        "pulled_date": "2026-09-03",
    }


UNIFACE_MODELS: tuple[LegacyModel, ...] = (
    LegacyModel(
        local_name="retinaface_mnet_v2_retinaface_mv2.onnx",
        model_name="uniface-retinaface-detect",
        version_label="uniface-2026-09",
        task_code="face_detection",
        description="RetinaFace (MobileNetV2 backbone) face detector with 5-point landmarks.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="3ca44c045651cabeed1193a1fae8946ad1f3a55da8fa74b341feab5a8319f757",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/retinaface_mv2.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes=(
            "uniface RetinaFaceWeights.MNET_V2 - smallest of 7 available RetinaFace sizes. "
            "Biometric-classified 2026-09-08: this manifest originally reserved that label "
            "for identity-embedding (recognition) models only, on the reasoning that a pure "
            "detector locates a face without identifying who it belongs to. "
            "test_biometric_models_keep_their_classification (pre-existing, written before "
            "this import) takes the broader, more conservative position that any "
            "face_detection/face_recognition/face_landmark/face_attribute task_code is "
            "biometric for privacy-review purposes - the classification is how a data-subject "
            "request or an incident responder finds every face-processing model in one query, "
            "regardless of whether it identifies anyone. That reasoning predates this import "
            "and matches this project's own conservative bias everywhere else biometric/face "
            "data is involved; loosening an existing test to fit a new import would have been "
            "the wrong direction to resolve the disagreement."
        ),
    ),
    LegacyModel(
        local_name="mobilenetv2_mobilenetv2.onnx",
        model_name="uniface-mobileface-recognition",
        version_label="uniface-2026-09",
        task_code="face_recognition",
        description="MobileFace (MobileNetV2 backbone) face recognition embedding model.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="38b148284dd48cc898d5d4453104252fbdcbacc105fe3f0b80e78954d9d20d89",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/mobilenetv2.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface MobileFaceWeights.MNET_V2. Recognition embeddings are biometric templates regardless of the model's own license - classified biometric here for the same reason InsightFace's recognition models are, so it is subject to the same review gate before any promotion.",
    ),
    LegacyModel(
        local_name="sphere20_sphere20.onnx",
        model_name="uniface-sphereface-recognition",
        version_label="uniface-2026-09",
        task_code="face_recognition",
        description="SphereFace (20-layer) face recognition embedding model.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="c02878cf658eb1861f580b7e7144b0d27cc29c440bcaa6a99d466d2854f14c9d",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/sphere20.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface SphereFaceWeights.SPHERE20. Biometric-classified for the same reason as MobileFace above.",
    ),
    LegacyModel(
        local_name="adaface_ir_18_adaface_ir_18.onnx",
        model_name="uniface-adaface-recognition",
        version_label="uniface-2026-09",
        task_code="face_recognition",
        description="AdaFace (IR-18 backbone) face recognition embedding model, adaptive margin loss.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="6b6a35772fb636cdd4fa86520c1a259d0c41472a76f70f802b351837a00d9870",
        legacy_paths=("https://github.com/yakhyo/adaface-onnx/releases/download/weights/adaface_ir_18.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface AdaFaceWeights.IR_18. Biometric-classified for the same reason as MobileFace above.",
    ),
    LegacyModel(
        local_name="edgeface_base_edgeface_base.onnx",
        model_name="uniface-edgeface-recognition",
        version_label="uniface-2026-09",
        task_code="face_recognition",
        description="EdgeFace (base) face recognition embedding model, designed for edge devices.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="b56942f072c67385f44734b9458b0ccc4a2226888a113f77e0c802ad0c77b4c3",
        legacy_paths=("https://github.com/yakhyo/edgeface-onnx/releases/download/weights/edgeface_base.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface EdgeFaceWeights.BASE. Biometric-classified for the same reason as MobileFace above.",
    ),
    LegacyModel(
        local_name="centerface_centerface.onnx",
        model_name="uniface-centerface-detect",
        version_label="uniface-2026-09",
        task_code="face_detection",
        description="CenterFace: anchor-free face detector (MobileNetV2 + FPN) with 5-point landmarks.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="f50be8b97eae35b969905619136765897e401171ab4f92aa2b8c9909292d2ba0",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/centerface.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface CenterFaceWeights.DEFAULT. Biometric-classified - see uniface-retinaface-detect's own notes for why a pure detector still gets the label.",
    ),
    LegacyModel(
        local_name="blazeface_face_detection_short_range.onnx",
        model_name="uniface-blazeface-detect",
        version_label="uniface-2026-09",
        task_code="face_detection",
        description="BlazeFace short-range: Google MediaPipe's SSD face detector on a 128x128 letterboxed image.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="2f2689b040becf555706d2cb978d2f0e3296ea82413734fba9a856c66c5f2b17",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/face_detection_short_range.onnx",),
        legacy_classes=(),
        license_metadata=_APACHE,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface BlazeFaceWeights.DEFAULT. Architecture and weights from Google MediaPipe. Biometric-classified - see uniface-retinaface-detect's own notes for why a pure detector still gets the label.",
    ),
    LegacyModel(
        local_name="pipnet_r18_wflw_98_pipnet_r18_wflw_98.onnx",
        model_name="uniface-pipnet-landmark",
        version_label="uniface-2026-09",
        task_code="face_landmark",
        description="PIPNet (ResNet-18, WFLW 98-point) facial landmark detector.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="9862838dc6144bc772b6485f6f6d31295c0b1c1ab7293e6ddeb0a439cb10218d",
        legacy_paths=("https://github.com/yakhyo/pipnet-onnx/releases/download/weights/pipnet_r18_wflw_98.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface PIPNetWeights.WFLW_98. Biometric-classified - see uniface-retinaface-detect's own notes for why a landmark localizer still gets the label.",
    ),
    LegacyModel(
        local_name="parsing_resnet18_resnet18.onnx",
        model_name="uniface-bisenet-parsing",
        version_label="uniface-2026-09",
        task_code="face_parsing",
        description="BiSeNet (ResNet-18) face parsing: per-pixel semantic segmentation of facial components.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="0d9bd318e46987c3bdbfacae9e2c0f461cae1c6ac6ea6d43bbe541a91727e33f",
        legacy_paths=("https://github.com/yakhyo/face-parsing/releases/download/weights/resnet18.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        initial_state="uploaded",
        notes="uniface ParsingWeights.RESNET18 (attribution table calls this family 'BiSeNet').",
    ),
    LegacyModel(
        local_name="gaze_resnet18_resnet18_gaze.onnx",
        model_name="uniface-mobilegaze-estimation",
        version_label="uniface-2026-09",
        task_code="gaze_estimation",
        description="MobileGaze (ResNet-18) real-time gaze direction estimation.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="404fec1efd07ff49f981e47f461c20c2627119e465ec441bbd1c067d3f16e657",
        legacy_paths=("https://github.com/yakhyo/gaze-estimation/releases/download/weights/resnet18_gaze.onnx",),
        legacy_classes=(),
        license_metadata=_MIT,
        initial_state="uploaded",
        notes="uniface GazeWeights.RESNET18 (attribution table calls this family 'MobileGaze').",
    ),
    LegacyModel(
        local_name="face_mesh_face_mesh_Nx3x192x192.onnx",
        model_name="uniface-facemesh-landmark",
        version_label="uniface-2026-09",
        task_code="face_landmark",
        description="MediaPipe Face Mesh V1: 468-point dense facial landmarks from a 192x192 crop.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="3ca77cf59c18e4da0eccb46695bf604683fa564253e3385892981a5c274fb10f",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/face_mesh_Nx3x192x192.onnx",),
        legacy_classes=(),
        license_metadata=_APACHE,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface FaceMeshWeights.V1_468. Topology and weights from Google MediaPipe. Biometric-classified - see uniface-retinaface-detect's own notes for why a landmark localizer still gets the label.",
    ),
    LegacyModel(
        local_name="modnet_photographic_modnet_photographic.onnx",
        model_name="uniface-modnet-matting",
        version_label="uniface-2026-09",
        task_code="portrait_matting",
        description="MODNet (photographic): real-time trimap-free portrait matting.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="5069a5e306b9f5e9f4f2b0360264c9f8ea13b257c7c39943c7cf6a2ec3a102ae",
        legacy_paths=("https://github.com/yakhyo/modnet/releases/download/weights/modnet_photographic.onnx",),
        legacy_classes=(),
        license_metadata=_APACHE,
        initial_state="uploaded",
        notes="uniface MODNetWeights.PHOTOGRAPHIC.",
    ),
    LegacyModel(
        local_name="minifasnet_v2_MiniFASNetV2.onnx",
        model_name="uniface-minifasnet-antispoofing",
        version_label="uniface-2026-09",
        task_code="face_anti_spoofing",
        description="MiniFASNet V2: lightweight face anti-spoofing (liveness) classifier.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907",
        legacy_paths=("https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx",),
        legacy_classes=(),
        license_metadata=_APACHE,
        initial_state="uploaded",
        notes="uniface MiniFASNetWeights.V2. Only useful paired with a recognition model - itself detects liveness, not identity.",
    ),
    LegacyModel(
        local_name="face_attrib_net_face_attrib_net.onnx",
        model_name="uniface-faceattribnet-attributes",
        version_label="uniface-2026-09",
        task_code="face_attribute",
        description="FaceAttribNet (Qualcomm): 5 binary face attributes - eye openness (L/R), eyeglasses, sunglasses, face mask.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="1bf7c6453bec2fb28e0830f3a76dceb9ffd020124f87b28da5355940a7bc6e48",
        legacy_paths=("https://github.com/yakhyo/uniface/releases/download/weights/face_attrib_net.onnx",),
        legacy_classes=(),
        license_metadata=_BSD,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface FaceAttribNetWeights.DEFAULT. Architecture and weights (c) Qualcomm Technologies, Inc. Biometric-classified - see uniface-retinaface-detect's own notes for why an attribute classifier still gets the label.",
    ),
    LegacyModel(
        local_name="fairface_fairface.onnx",
        model_name="uniface-fairface-attributes",
        version_label="uniface-2026-09",
        task_code="face_attribute",
        description="FairFace: race/gender/age attribute prediction, trained on a demographically balanced dataset.",
        framework="onnx",
        runtime="onnxruntime",
        sha256="9c8c47d437cd310538d233f2465f9ed0524cb7fb51882a37f74e8bc22437fdbf",
        legacy_paths=("https://github.com/yakhyo/fairface-onnx/releases/download/weights/fairface.onnx",),
        legacy_classes=(),
        license_metadata=_CC_BY,
        access_classification="biometric",
        initial_state="uploaded",
        notes="uniface FairFaceWeights.DEFAULT. CC BY 4.0 - attribution required wherever its output is shown, not just in code. Biometric-classified - see uniface-retinaface-detect's own notes for why an attribute classifier still gets the label.",
    ),
)
