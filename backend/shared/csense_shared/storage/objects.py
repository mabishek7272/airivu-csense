"""MinIO-compatible object storage (SCH §14, TRD §14).

Bucket layout and lifecycle come straight from SCH §14. Two rules drive this module:

  - **Clients never build object keys.** Keys are opaque and tenant-prefixed, and are
    produced here so the prefix convention cannot drift or be influenced by user input.
  - **Model artifacts are content-addressed.** The key embeds the SHA-256 digest, so the
    same bytes always land at the same key and a published version can never be
    silently repointed at different content (TRD §15.2).

Bucket policies deny public access; readers get scoped pre-signed URLs only after an
authorization check in the calling service (TRD §14).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from minio import Minio

from csense_shared.config import Settings

BUCKET_EVIDENCE = "csense-evidence"
BUCKET_CLIPS = "csense-clips"
BUCKET_MODELS = "csense-models"
BUCKET_EXPORTS = "csense-exports"
BUCKET_DIAGNOSTICS = "csense-diagnostics"
BUCKET_AUDIT_ARCHIVE = "csense-audit-archive"
BUCKET_BACKUPS = "csense-backups"

ALL_BUCKETS = (
    BUCKET_EVIDENCE,
    BUCKET_CLIPS,
    BUCKET_MODELS,
    BUCKET_EXPORTS,
    BUCKET_DIAGNOSTICS,
    BUCKET_AUDIT_ARCHIVE,
    BUCKET_BACKUPS,
)


def create_client(settings: Settings) -> Minio:
    """Client for server-side operations: uploads, stat, bucket management."""
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        secure=settings.minio_use_tls,
    )


def create_presign_client(settings: Settings) -> Minio:
    """Client used only to mint presigned URLs.

    A presigned URL is signed for a specific host, and it is a browser that will follow
    it - so it must name the externally reachable endpoint, not the container-network
    name the API itself connects to. Signing with the internal name produces URLs that
    work from inside the stack and fail in every browser, which server-side tests do not
    catch.

    `region` is pinned rather than discovered. Signing is a pure cryptographic operation
    and needs no network, but the SDK will call GetBucketLocation first unless the region
    is already known - and that call would go to the *public* endpoint, which this process
    generally cannot reach from inside the container network.
    """
    return Minio(
        settings.minio_presign_endpoint,
        access_key=settings.minio_root_user,
        secret_key=settings.minio_root_password,
        secure=settings.minio_use_tls,
        region=settings.minio_region,
    )


def ensure_buckets(client: Minio) -> list[str]:
    """Creates any missing buckets. Idempotent; returns the ones actually created."""
    created = []
    for bucket in ALL_BUCKETS:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            created.append(bucket)
    return created


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_artifact_key(model_name: str, version_label: str, sha256: str, extension: str) -> str:
    """SCH §14: `global/models/{model}/{version}/{sha256}.onnx`.

    Content-addressed: the digest is part of the key, so re-uploading identical bytes is a
    no-op rather than an overwrite, and differing bytes can never occupy the same key.
    """
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"global/models/{model_name}/{version_label}/{sha256}{ext}"


def tenant_evidence_key(tenant_id: UUID, incident_id: UUID, evidence_id: UUID, variant: str) -> str:
    """SCH §14: `{tenant}/incidents/{incident}/{evidence_id}/{variant}.jpg`."""
    return f"{tenant_id}/incidents/{incident_id}/{evidence_id}/{variant}.jpg"


@dataclass(frozen=True)
class UploadResult:
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int
    already_present: bool


def upload_model_artifact(
    client: Minio,
    *,
    local_path: str | Path,
    model_name: str,
    version_label: str,
    extension: str | None = None,
) -> UploadResult:
    """Uploads a model artifact to its content-addressed key.

    If an object already exists at that key its digest is, by construction, identical, so
    the upload is skipped rather than repeated — re-running an import is safe.
    """
    path = Path(local_path)
    digest = sha256_file(path)
    size = path.stat().st_size
    ext = extension if extension is not None else path.suffix
    key = model_artifact_key(model_name, version_label, digest, ext)

    try:
        client.stat_object(BUCKET_MODELS, key)
        return UploadResult(BUCKET_MODELS, key, digest, size, already_present=True)
    except Exception:
        # Not present (or not readable as an existing object) — upload it.
        pass

    client.fput_object(BUCKET_MODELS, key, str(path))

    # Verify what actually landed, rather than trusting the client's success return.
    stat = client.stat_object(BUCKET_MODELS, key)
    if stat.size != size:
        raise RuntimeError(
            f"Upload size mismatch for {key}: expected {size} bytes, storage reports {stat.size}"
        )
    return UploadResult(BUCKET_MODELS, key, digest, size, already_present=False)
