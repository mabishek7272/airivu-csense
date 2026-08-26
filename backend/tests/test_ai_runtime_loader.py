"""Artifact cache and model pool behaviour.

The property that matters most here is that the runtime refuses to load bytes that do not
match the digest the registry recorded (TRD §12.4). A model that silently loads tampered
or corrupted weights would produce plausible-looking detections that are quietly wrong,
which is worse than an outage.

These use a fake object-storage client, so they run without Docker or MinIO.
"""
from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path

import pytest

# The ai_runtime service package is not installed into the test environment; it is copied
# to /app/app in its image. Add it to the path so `app.loader` resolves here.
AI_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "ai_runtime"
if str(AI_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_RUNTIME_ROOT))

from app.loader import ArtifactVerificationError, ModelArtifactCache  # noqa: E402


class FakeMinio:
    """Stands in for a Minio client, writing predetermined bytes on fget_object."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads
        self.fetch_count = 0
        self.fetched_keys: list[str] = []

    def fget_object(self, bucket: str, object_key: str, file_path: str) -> None:
        self.fetch_count += 1
        self.fetched_keys.append(object_key)
        Path(file_path).write_bytes(self._payloads[object_key])


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_downloads_and_caches_artifact(tmp_path):
    payload = b"model-weights-v1"
    digest = _digest(payload)
    client = FakeMinio({"global/models/m/v1/x.onnx": payload})
    cache = ModelArtifactCache(client, tmp_path)

    path = cache.ensure_local(
        bucket="csense-models", object_key="global/models/m/v1/x.onnx", digest=digest, extension=".onnx"
    )

    assert path.exists()
    assert path.read_bytes() == payload
    assert client.fetch_count == 1


def test_second_call_uses_cache(tmp_path):
    payload = b"model-weights-v1"
    digest = _digest(payload)
    client = FakeMinio({"k": payload})
    cache = ModelArtifactCache(client, tmp_path)

    for _ in range(3):
        cache.ensure_local(bucket="b", object_key="k", digest=digest, extension=".onnx")

    assert client.fetch_count == 1, "cached artifact should not be re-downloaded"


def test_digest_mismatch_is_rejected_and_artifact_removed(tmp_path):
    """The core safety property: bytes that are not what the registry described must not
    be loaded, and must not be left on disk where a later run could trust them."""
    client = FakeMinio({"k": b"tampered-weights"})
    cache = ModelArtifactCache(client, tmp_path)
    expected = _digest(b"the-real-weights")

    with pytest.raises(ArtifactVerificationError) as excinfo:
        cache.ensure_local(bucket="b", object_key="k", digest=expected, extension=".onnx")

    assert expected[:12] in str(excinfo.value)
    assert list(tmp_path.iterdir()) == [], "failed artifact must not remain on disk"


def test_corrupted_cache_file_is_refetched(tmp_path):
    payload = b"model-weights-v1"
    digest = _digest(payload)
    client = FakeMinio({"k": payload})
    cache = ModelArtifactCache(client, tmp_path)

    path = cache.ensure_local(bucket="b", object_key="k", digest=digest, extension=".onnx")
    path.write_bytes(b"corrupted-on-disk")

    refetched = cache.ensure_local(bucket="b", object_key="k", digest=digest, extension=".onnx")

    assert refetched.read_bytes() == payload
    assert client.fetch_count == 2, "a cache file whose digest no longer matches must be refetched"


def test_concurrent_requests_download_once(tmp_path):
    """Two requests for the same cold model should not both download it."""
    payload = b"model-weights-v1"
    digest = _digest(payload)
    client = FakeMinio({"k": payload})
    cache = ModelArtifactCache(client, tmp_path)

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            cache.ensure_local(bucket="b", object_key="k", digest=digest, extension=".onnx")
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent loads raised: {errors}"
    assert client.fetch_count == 1, f"expected a single download, got {client.fetch_count}"


def test_cache_path_is_content_addressed(tmp_path):
    """Distinct artifacts must never share a cache path, even across models."""
    cache = ModelArtifactCache(FakeMinio({}), tmp_path)
    a = cache.path_for(_digest(b"a"), ".pt")
    b = cache.path_for(_digest(b"b"), ".pt")
    assert a != b
