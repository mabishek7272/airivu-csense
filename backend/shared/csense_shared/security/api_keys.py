"""API key generation and hashing (SCH §10.5 `api_clients`/`api_keys`) - the same
prefix + SHA-256-digest lookup shape `deps_agent.py` already established for device
credentials, applied to a second, unrelated kind of caller (an external integration,
not a device). A high-entropy random secret gets a fast digest, not Argon2id - that
slowness exists specifically to blunt guessing a human-memorable password; a 256-bit
random token has nothing for a fast hash to make guessable.
"""
from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX_LENGTH = 8


def generate_api_key() -> tuple[str, str, str]:
    """Returns `(full_key, prefix, digest)` - `full_key` is shown to the caller exactly
    once; only `prefix` (for lookup) and `digest` (for comparison) are ever stored."""
    full_key = f"csak_{secrets.token_urlsafe(32)}"
    prefix = full_key[:KEY_PREFIX_LENGTH]
    digest = hash_api_key(full_key)
    return full_key, prefix, digest


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()
