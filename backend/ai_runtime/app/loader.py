"""Fetches model artifacts from object storage and caches them on local disk.

Two rules, both from docs/02_TECHNICAL_REQUIREMENTS_DOCUMENT.md:

  - **Verify before activation** (TRD §12.4: "Model artifact digest verification before
    activation"). Bytes are hashed after download and compared to the digest the registry
    recorded. A mismatch means the artifact is not what was reviewed, so it is deleted and
    the load fails rather than falling back to running it anyway.

  - **Content-addressed cache.** The cache path embeds the digest, so two versions can
    never collide, a corrupted file can never masquerade as a good one, and re-downloading
    is skipped only when the bytes on disk actually hash to what we want.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from minio import Minio

from csense_shared.storage.objects import sha256_file

logger = logging.getLogger(__name__)


class ArtifactVerificationError(RuntimeError):
    """Raised when downloaded bytes do not match the registry's recorded digest."""


class ModelArtifactCache:
    """Thread-safe, content-addressed local cache of model artifacts."""

    def __init__(self, client: Minio, cache_dir: str | Path) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # One lock per digest: two concurrent requests for the same cold model should
        # download once, not race each other writing the same file.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, digest: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(digest, threading.Lock())

    def path_for(self, digest: str, extension: str) -> Path:
        ext = extension if extension.startswith(".") else f".{extension}"
        return self._cache_dir / f"{digest}{ext}"

    def ensure_local(self, *, bucket: str, object_key: str, digest: str, extension: str) -> Path:
        """Returns a local path holding exactly the bytes `digest` describes.

        Downloads on a cache miss. Raises ArtifactVerificationError if what arrives does
        not hash to `digest` — the artifact is removed rather than left on disk where a
        later run might trust it.
        """
        target = self.path_for(digest, extension)

        if target.exists() and sha256_file(target) == digest:
            return target

        with self._lock_for(digest):
            # Re-check inside the lock: another thread may have fetched it while we waited.
            if target.exists() and sha256_file(target) == digest:
                return target

            partial = target.with_suffix(target.suffix + ".partial")
            logger.info("fetching model artifact", extra={"object_key": object_key, "digest": digest[:12]})
            self._client.fget_object(bucket, object_key, str(partial))

            actual = sha256_file(partial)
            if actual != digest:
                partial.unlink(missing_ok=True)
                raise ArtifactVerificationError(
                    f"Digest mismatch for {object_key}: registry says {digest}, downloaded bytes are {actual}. "
                    "Refusing to load."
                )

            # Atomic swap, so a reader never observes a half-written file at the real path.
            partial.replace(target)
            logger.info("model artifact cached", extra={"digest": digest[:12], "path": str(target)})
            return target

    def cached_digests(self) -> set[str]:
        return {p.stem for p in self._cache_dir.iterdir() if p.is_file() and not p.name.endswith(".partial")}
