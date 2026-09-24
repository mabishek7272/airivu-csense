"""Face template encoding and matching.

A face template is a unit-length 512-d float32 vector (`ai_runtime`'s `/internal/v1/infer-
face` already L2-normalises), so the cosine similarity of two templates is just their dot
product. Templates are stored encrypted (`encrypted_secrets`, purpose `face.template`) as
base64 of the raw float32 bytes - 2 KB, ~2.7 KB encoded, well under the 8 KB envelope cap.

**Three outcomes, chosen so a rule can key on a plain class string:**

  `face:known`      matched a registered person on the `known` list
  `face:watchlist`  matched a registered person on the `watchlist`
  `face:unknown`    matched nobody at or above the threshold

Rules match `alertable_classes` by exact string (`pipeline/rules.py`), so an "alert on
unknown faces" rule lists `face:unknown` and a watchlist rule lists `face:watchlist`; a
known person is recorded as a detection (who was here) but matches no alerting rule.

**Threshold.** ArcFace cosine similarity for the same person is typically above ~0.4-0.5
and for different people below ~0.3. 0.40 is a starting point, not a validated figure: it
is exposed as a per-assignment override precisely because it has to be tuned against real
footage from the actual camera.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import numpy as np

EMBEDDING_DIM = 512
DEFAULT_THRESHOLD = 0.40

CLASS_KNOWN = "face:known"
CLASS_WATCHLIST = "face:watchlist"
CLASS_UNKNOWN = "face:unknown"


class TemplateError(ValueError):
    """A vector that cannot be a face template (wrong size, NaN, zero length)."""


def normalise(vector: np.ndarray) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32).reshape(-1)
    if v.shape[0] != EMBEDDING_DIM:
        raise TemplateError(f"expected a {EMBEDDING_DIM}-d embedding, got {v.shape[0]}")
    if not np.all(np.isfinite(v)):
        raise TemplateError("embedding contains NaN or infinity")
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        raise TemplateError("embedding has zero length")
    return v / norm


def encode_template(vector: np.ndarray) -> str:
    return base64.b64encode(normalise(vector).astype("<f4").tobytes()).decode("ascii")


def decode_template(encoded: str | bytes) -> np.ndarray:
    raw = base64.b64decode(encoded)
    if len(raw) != EMBEDDING_DIM * 4:
        raise TemplateError(f"stored template is {len(raw)} bytes, expected {EMBEDDING_DIM * 4}")
    return normalise(np.frombuffer(raw, dtype="<f4"))


@dataclass(frozen=True)
class GalleryEntry:
    identity_id: str
    name: str
    list_type: str  # "known" | "watchlist"
    vector: np.ndarray  # unit length


@dataclass(frozen=True)
class Match:
    identity_id: str
    name: str
    list_type: str
    similarity: float


def best_match(
    embedding: np.ndarray, gallery: list[GalleryEntry], threshold: float = DEFAULT_THRESHOLD
) -> Match | None:
    """The single most similar registered person at or above `threshold`, or None.

    An identity may have several templates (different photos); each is a row in
    `gallery`, and the identity's score is its best template.
    """
    if not gallery:
        return None
    query = normalise(embedding)
    matrix = np.stack([g.vector for g in gallery])
    scores = matrix @ query
    index = int(np.argmax(scores))
    score = float(scores[index])
    if score < threshold:
        return None
    entry = gallery[index]
    return Match(entry.identity_id, entry.name, entry.list_type, round(score, 4))


def classify(
    embedding: np.ndarray, gallery: list[GalleryEntry], threshold: float = DEFAULT_THRESHOLD
) -> tuple[str, dict | None]:
    """`(class_name, attributes)` for one detected face."""
    match = best_match(embedding, gallery, threshold)
    if match is None:
        return CLASS_UNKNOWN, None
    class_name = CLASS_WATCHLIST if match.list_type == "watchlist" else CLASS_KNOWN
    return class_name, {
        "identity_id": match.identity_id,
        "name": match.name,
        "similarity": match.similarity,
    }
