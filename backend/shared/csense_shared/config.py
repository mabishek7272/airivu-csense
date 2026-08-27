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
    # Presigned URLs are handed to a browser, so they must name a host the browser
    # can reach - not the container-network name the services connect to. Left blank,
    # presigning falls back to `minio_endpoint`, which is correct only when the two
    # are the same host.
    minio_public_endpoint: str = ""
    # Pinned so presigning never needs a GetBucketLocation round trip. MinIO
    # defaults to us-east-1 unless configured otherwise.
    minio_region: str = "us-east-1"

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

    # --- Notification providers ---
    # Each is optional: a deployment without WhatsApp is valid, and an unconfigured
    # channel reports itself rather than crashing at send time.
    resend_api_key: str = ""
    resend_from_address: str = "alerts@example.invalid"
    # Points at a Cloudflare Email Routing address, so replying to an alert reaches the
    # inbound webhook rather than an unwatched mailbox.
    resend_reply_to: str = ""

    whatsapp_gateway_url: str = ""
    # Admin key (the gateway's GLOBAL_API_KEY): instance create/list/delete only.
    whatsapp_gateway_api_key: str = ""
    # Instance token: everything that acts as the linked number - status, QR, send.
    # The gateway identifies the instance *by* this token, so the two are not
    # interchangeable. Supplied by us at instance creation.
    whatsapp_instance_token: str = ""
    whatsapp_instance: str = "default"

    # Envelope encryption for stored credentials (camera RTSP passwords and the like).
    # A directory rather than a single file, so a rotation is "add the new key, restart,
    # rewrap" instead of a flag day - old secrets stay readable under the old key.
    master_key_dir: str = "/run/secrets"
    # Which key new secrets are sealed under. Empty means the highest-numbered one found,
    # so adding master_v2.key is enough to start using it.
    master_key_active_id: str = ""

    # Notification worker. The poll interval bounds how late an alert can be, so it is
    # deliberately short - the query is indexed on next_attempt_at and costs almost
    # nothing when the queue is empty. The batch size bounds how much work one crash can
    # leave stranded in `sending` for the stalled-delivery sweep to recover.
    notification_poll_seconds: float = 5.0
    notification_batch_size: int = 25

    # CORS origins
    # Comma-separated allow-lists. Each app is reachable at more than one origin:
    # through Traefik in the container stack, and on a Vite/Next dev port locally.
    # An allow-list, never a wildcard - these APIs use cookie auth, and
    # `Access-Control-Allow-Origin: *` is incompatible with credentialed requests
    # for good reason (TRD-SEC-006).
    customer_crm_origin: str = "http://app.localhost:8080,http://localhost:5173"
    developer_console_origin: str = "http://console.localhost:8080,http://localhost:3000"

    # AI runtime. Cache lives on a volume so restarts do not re-download ~550 MB of
    # artifacts; pool size bounds resident models by count, since in-memory footprint is
    # framework-dependent and not predictable from file size.
    model_cache_dir: str = "/var/cache/csense/models"
    model_pool_size: int = 4

    @property
    def minio_presign_endpoint(self) -> str:
        return self.minio_public_endpoint or self.minio_endpoint

    @property
    def customer_crm_origins(self) -> list[str]:
        return [o.strip() for o in self.customer_crm_origin.split(",") if o.strip()]

    @property
    def developer_console_origins(self) -> list[str]:
        return [o.strip() for o in self.developer_console_origin.split(",") if o.strip()]

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
