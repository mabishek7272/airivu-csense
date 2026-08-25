from csense_shared.security.passwords import hash_password, verify_password


def test_hash_and_verify_roundtrip(settings):
    hashed = hash_password("correct horse battery staple 42", settings)
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse battery staple 42", hashed, settings)


def test_verify_rejects_wrong_password(settings):
    hashed = hash_password("correct horse battery staple 42", settings)
    assert not verify_password("wrong password entirely", hashed, settings)


def test_hash_uses_configured_argon2id_baseline(settings):
    hashed = hash_password("correct horse battery staple 42", settings)
    # TRD-SEC baseline: time_cost >= 3, memory_cost >= 64MB (65536 KB).
    assert "m=65536" in hashed
    assert "t=3" in hashed
