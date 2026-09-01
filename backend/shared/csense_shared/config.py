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

    # MediaMTX (live view)
    # Control API, reached container-to-container - never exposed to a browser.
    mediamtx_control_url: str = "http://mediamtx:9997"
    # Same "not the container-network name" reasoning as `minio_public_endpoint`: the
    # play_url handed to a browser has to name a host it can actually reach, i.e. through
    # Traefik, not the `mediamtx` service name.
    media_public_base_url: str = "http://app.localhost:8080"

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

    # Networks this host can already reach directly - its Docker bridges, its own LAN.
    # A tenant must never be able to allowlist one of these for tunnel access: traffic to
    # such an address takes the local route, not their tunnel, so "reach my camera at
    # 172.18.0.5" would reach our own Postgres instead. Comma-separated CIDRs.
    #
    # Deliberately narrow, not "all of RFC1918": the whole point of this check is to catch
    # the *specific* ranges this host's own networking uses, and 10.0.0.0/8 plus
    # 192.168.0.0/16 are exactly the address spaces the WireGuard deployment guide expects
    # tenants to declare as their camera-side LAN. A default this broad would silently
    # reject every legitimate site LAN a tenant could ever provision - a self-defeating
    # default for a check whose entire purpose is to let tunnels through while catching
    # the one that isn't one. Set this to whatever the deployment's actual bridge and LAN
    # ranges are; the two given here are Docker's own default bridge and this project's
    # Compose-created one.
    reserved_local_networks: str = "172.17.0.0/16,172.18.0.0/16"

    # WireGuard: the one shared tunnel every tenant's edge devices provision into. A
    # device's /32 is allocated from this pool (see vpn_pool.py); the server identity below
    # is what a generated client config points at. Both are blank by default - a local dev
    # stack has no real WireGuard server, and a rendered config says so rather than
    # emitting a plausible-looking but useless placeholder.
    wireguard_pool_cidr: str = "10.8.0.0/16"
    wireguard_server_public_key: str = ""
    wireguard_server_endpoint: str = ""
    # The server's own address inside the tunnel, e.g. "10.8.0.1/32" - what a device's
    # client config routes to. Deliberately not the whole pool: a device only ever talks to
    # the server, and giving its own routing table a claim on every other peer's address is
    # the same over-broad-AllowedIPs mistake this whole mechanism exists to avoid, just on
    # the client side instead of the server's.
    wireguard_server_address: str = ""

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

    # How old an outbox event may be and still be worth delivering as a webhook. Bounds
    # two real cases with one rule: on first deploy the entire pre-existing outbox history
    # is outside the window, so a brand-new endpoint is never blasted with months of past
    # events; and after a long worker outage only recent events are delivered rather than
    # a flood of stale ones. Deliberately different from the platform's usual "late alert
    # beats no alert" stance (MAX_DELAY_SECONDS, the escalation ladder) - a webhook is an
    # integration feed, and a day-old "incident created" POST is noise to a receiver, not
    # a late alert to a human.
    webhook_dispatch_max_event_age_seconds: float = 24 * 60 * 60

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
