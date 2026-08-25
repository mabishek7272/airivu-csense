from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def jwt_keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Generates a throwaway RSA keypair for token tests — never reused outside the
    test process, distinct from any real dev/prod keypair."""
    key_dir = tmp_path_factory.mktemp("jwt-keys")
    private_path = key_dir / "private.pem"
    public_path = key_dir / "public.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(private_path), "2048"], check=True, capture_output=True)
    subprocess.run(
        ["openssl", "rsa", "-in", str(private_path), "-pubout", "-out", str(public_path)],
        check=True,
        capture_output=True,
    )
    return private_path, public_path


@pytest.fixture()
def settings(jwt_keypair):
    from csense_shared.config import Settings

    private_path, public_path = jwt_keypair
    return Settings(
        postgres_password="test",
        mongo_password="test",
        redis_password="test",
        minio_root_user="test",
        minio_root_password="test",
        jwt_private_key_path=str(private_path),
        jwt_public_key_path=str(public_path),
    )


def postgres_available() -> bool:
    return bool(os.environ.get("TEST_POSTGRES_DSN"))
