"""API key generation/hashing (csense_shared.security.api_keys) - no I/O, pure logic."""
from __future__ import annotations

from csense_shared.security.api_keys import generate_api_key, hash_api_key


def test_generated_key_has_the_expected_prefix_shape():
    full_key, prefix, digest = generate_api_key()
    assert full_key.startswith("csak_")
    assert prefix == full_key[:8]
    assert digest == hash_api_key(full_key)


def test_two_generated_keys_never_collide():
    first, _, _ = generate_api_key()
    second, _, _ = generate_api_key()
    assert first != second


def test_hash_is_deterministic_for_the_same_input():
    full_key, _, digest = generate_api_key()
    assert hash_api_key(full_key) == digest


def test_hash_differs_for_different_keys():
    a, _, _ = generate_api_key()
    b, _, _ = generate_api_key()
    assert hash_api_key(a) != hash_api_key(b)
