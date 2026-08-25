"""MongoDB client + tenant-filtering query helpers.

SCH §2 / TRD §8.1: MongoDB is never trusted to infer tenant ownership from an opaque
object ID. Every collection access in this module requires an explicit tenant_id and
folds it into the filter — there is no "raw" query helper exposed to callers.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from csense_shared.config import Settings


def create_mongo_client(settings: Settings) -> AsyncIOMotorClient:
    return AsyncIOMotorClient(settings.mongo_dsn, uuidRepresentation="standard")


def get_database(client: AsyncIOMotorClient, settings: Settings) -> AsyncIOMotorDatabase:
    return client[settings.mongo_db]


class TenantScopedCollection:
    """Thin wrapper that forces every read/write through a `tenant_id` filter."""

    def __init__(self, db: AsyncIOMotorDatabase, collection_name: str, tenant_id: UUID):
        self._collection = db[collection_name]
        self._tenant_id = str(tenant_id)

    def _scoped(self, query: Mapping[str, Any] | None) -> dict[str, Any]:
        scoped = dict(query or {})
        scoped["tenant_id"] = self._tenant_id
        return scoped

    async def find_one(self, query: Mapping[str, Any] | None = None):
        return await self._collection.find_one(self._scoped(query))

    def find(self, query: Mapping[str, Any] | None = None, **kwargs):
        return self._collection.find(self._scoped(query), **kwargs)

    async def insert_one(self, document: dict[str, Any]):
        document = dict(document)
        document["tenant_id"] = self._tenant_id
        return await self._collection.insert_one(document)

    async def count_documents(self, query: Mapping[str, Any] | None = None) -> int:
        return await self._collection.count_documents(self._scoped(query))
