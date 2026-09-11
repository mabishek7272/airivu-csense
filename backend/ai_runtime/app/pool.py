"""Keeps loaded models resident in memory.

Loading a YOLOv8-medium checkpoint takes seconds; doing that per request would put model
load time on the detection path and blow the p95 <5s event-to-alert budget (TRD §22.2).
Models are therefore loaded once and kept.

Bounded by count rather than bytes: the estate's artifacts range from 1.3 MB to 166 MB and
in-memory footprint is larger and framework-dependent, so a byte budget computed from file
size would be misleading. Eviction is least-recently-used.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.engines import EngineInfo, build_engine
from app.loader import ModelArtifactCache
from app.registry import RegisteredModel

logger = logging.getLogger(__name__)


@dataclass
class LoadedModel:
    model_name: str
    version_id: str
    artifact_sha256: str
    engine: object
    info: EngineInfo
    loaded_at: float
    load_seconds: float
    inference_count: int = 0


class ModelPool:
    def __init__(self, cache: ModelArtifactCache, max_resident: int = 4) -> None:
        self._cache = cache
        self._max_resident = max_resident
        self._models: OrderedDict[str, LoadedModel] = OrderedDict()
        self._lock = threading.Lock()
        # Per-model locks so a slow load of one model does not block loads of others.
        self._load_locks: dict[str, threading.Lock] = {}
        self._load_locks_guard = threading.Lock()

    def _load_lock_for(self, digest: str) -> threading.Lock:
        with self._load_locks_guard:
            return self._load_locks.setdefault(digest, threading.Lock())

    def get(self, registered: RegisteredModel) -> LoadedModel:
        """Returns a resident model, loading it if necessary.

        Keyed on artifact digest, not model name: if a model is ever repointed at
        different weights, the key changes and the stale engine is never reused.
        """
        key = registered.artifact_sha256

        with self._lock:
            existing = self._models.get(key)
            if existing is not None:
                self._models.move_to_end(key)
                return existing

        with self._load_lock_for(key):
            # Re-check: another thread may have finished loading while we waited.
            with self._lock:
                existing = self._models.get(key)
                if existing is not None:
                    self._models.move_to_end(key)
                    return existing

            started = time.monotonic()
            path = self._cache.ensure_local(
                bucket=registered.bucket,
                object_key=registered.object_key,
                digest=registered.artifact_sha256,
                extension=registered.extension,
            )
            label_map = None
            if registered.label_map:
                # label_map arrives from JSONB with string keys.
                label_map = {int(k): v for k, v in registered.label_map.items()}

            engine = build_engine(
                registered.runtime, path, label_map, registered.task_code, registered.model_name
            )
            loaded = LoadedModel(
                model_name=registered.model_name,
                version_id=str(registered.version_id),
                artifact_sha256=key,
                engine=engine,
                info=engine.info(),
                loaded_at=time.time(),
                load_seconds=time.monotonic() - started,
            )
            logger.info(
                "model loaded",
                extra={
                    "model": registered.model_name,
                    "runtime": registered.runtime,
                    "load_seconds": round(loaded.load_seconds, 2),
                },
            )

            with self._lock:
                self._models[key] = loaded
                self._models.move_to_end(key)
                while len(self._models) > self._max_resident:
                    evicted_key, evicted = self._models.popitem(last=False)
                    logger.info("model evicted from pool", extra={"model": evicted.model_name})
            return loaded

    def resident(self) -> list[LoadedModel]:
        with self._lock:
            return list(self._models.values())

    def is_resident(self, artifact_sha256: str) -> bool:
        with self._lock:
            return artifact_sha256 in self._models
