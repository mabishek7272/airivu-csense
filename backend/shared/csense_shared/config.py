"""Central settings, loaded from environment variables (populated from .env in Compose).

Per TRD-SEC-003, no secret ever has a hardcoded default here — anything sensitive is
required and comes from the environment / mounted Docker secret files.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "local"
    log_level: str = "INFO"

    # PostgreSQL
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "csense"
    postgres_user: str = "csense_app"
    postgres_password: str = Field(...)


    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = Field(...)

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_root_user: str = Field(...)
    minio_root_password: str = Field(...)
    minio_use_tls: bool = False

    # JWT
    jwt_algorithm: str = "RS256"
    jwt_private_key_path: str = "/run/secrets/jwt_private.pem"
    jwt_public_key_path: str = "/run/secrets/jwt_public.pem"
    jwt_issuer: str = "csense-local"
    jwt_access_token_ttl_seconds: int = 600
    jwt_refresh_token_ttl_seconds: int = 1_209_600

    # Argon2id (TRD-SEC baseline: time cost >= 3, memory >= 64MB)
    argon2_time_cost: int = 3
    argon2_memory_cost_kb: int = 65536
    argon2_parallelism: int = 2

    # CORS origins
    customer_crm_origin: str = "http://localhost:5173"
    developer_console_origin: str = "http://localhost:3000"

    # AI runtime. Cache lives on a volume so restarts do not re-download ~550 MB of
    # artifacts; pool size bounds resident models by count, since in-memory footprint is
    # framework-dependent and not predictable from file size.
    model_cache_dir: str = "/var/cache/csense/models"
    model_pool_size: int = 4

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def jwt_private_key(self) -> str:
        return Path(self.jwt_private_key_path).read_text()

    @property
    def jwt_public_key(self) -> str:
        return Path(self.jwt_public_key_path).read_text()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
