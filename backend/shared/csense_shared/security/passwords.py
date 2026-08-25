"""Argon2id password hashing (TRD-SEC baseline: time cost >= 3, memory >= 64 MB)."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from csense_shared.config import Settings


def _hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kb,
        parallelism=settings.argon2_parallelism,
    )


def hash_password(password: str, settings: Settings) -> str:
    return _hasher(settings).hash(password)


def verify_password(password: str, password_hash: str, settings: Settings) -> bool:
    try:
        _hasher(settings).verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str, settings: Settings) -> bool:
    """True if stored hash params are weaker than current policy — caller should
    re-hash on next successful login (annual parameter review, TRD-SEC §7.1)."""
    return _hasher(settings).check_needs_rehash(password_hash)
