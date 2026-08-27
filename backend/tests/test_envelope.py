"""Envelope encryption: the properties that make stored credentials safe.

Happy-path encrypt/decrypt is the least interesting thing here. What matters is what
happens when someone with database access tries to misuse the ciphertext, because that is
the actual threat model: a backup, a replica, or a SQL read that should not have been
possible.

The binding tests are the important ones. Without AES-GCM additional authenticated data,
an attacker who can write to the database could copy tenant A's encrypted camera password
into tenant B's camera row, and the system would decrypt it for them. That is
privilege escalation across tenants achieved without ever breaking any crypto.

No database, no network.
"""
from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest

from csense_shared.security.envelope import (
    KEY_BYTES,
    EnvelopeError,
    KeyRing,
    SealedSecret,
    generate_master_key,
    open_secret,
    rewrap,
    seal,
)

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
SECRET_ID = uuid.uuid4()
PASSWORD = "NvrPassw0rd!with-symbols-$&"


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing({"v1": generate_master_key()}, "v1")


def roundtrip(keyring: KeyRing, value=PASSWORD, **ctx) -> bytes:
    context = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    context.update(ctx)
    sealed = seal(keyring, value, **context)
    return open_secret(keyring, sealed, **context)


# --- Basics ---------------------------------------------------------------------------

def test_roundtrip(keyring):
    assert roundtrip(keyring).decode() == PASSWORD


def test_unicode_and_symbols_survive(keyring):
    """Camera passwords contain the characters people reach for when told to add one."""
    value = "pässwörd-£$%^&*()_+{}|:<>?~`"
    assert roundtrip(keyring, value).decode() == value


def test_ciphertext_does_not_contain_the_plaintext(keyring):
    sealed = seal(
        keyring, PASSWORD, tenant_id=TENANT, secret_id=SECRET_ID, purpose="camera.rtsp"
    )
    blob = b"".join(
        [sealed.ciphertext, sealed.wrapped_dek, sealed.dek_nonce, sealed.ciphertext_nonce]
    )
    assert PASSWORD.encode() not in blob


def test_same_value_encrypts_differently_each_time(keyring):
    """Deterministic ciphertext would let anyone with read access see which cameras share
    a password - a real finding on its own, and a stepping stone to worse."""
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    a = seal(keyring, PASSWORD, **ctx)
    b = seal(keyring, PASSWORD, **ctx)

    assert a.ciphertext != b.ciphertext
    assert a.ciphertext_nonce != b.ciphertext_nonce
    assert a.wrapped_dek != b.wrapped_dek


def test_each_secret_gets_its_own_data_key(keyring):
    ctx = {"tenant_id": TENANT, "purpose": "camera.rtsp"}
    a = seal(keyring, PASSWORD, secret_id=uuid.uuid4(), **ctx)
    b = seal(keyring, PASSWORD, secret_id=uuid.uuid4(), **ctx)

    assert a.wrapped_dek != b.wrapped_dek


# --- Context binding: the anti-tenant-hopping property ---------------------------------

def test_ciphertext_cannot_be_moved_to_another_tenant(keyring):
    """The attack this design exists to stop."""
    sealed = seal(
        keyring, PASSWORD, tenant_id=TENANT, secret_id=SECRET_ID, purpose="camera.rtsp"
    )

    with pytest.raises(EnvelopeError):
        open_secret(
            keyring, sealed,
            tenant_id=OTHER_TENANT, secret_id=SECRET_ID, purpose="camera.rtsp",
        )


def test_ciphertext_cannot_be_moved_to_another_row(keyring):
    sealed = seal(
        keyring, PASSWORD, tenant_id=TENANT, secret_id=SECRET_ID, purpose="camera.rtsp"
    )

    with pytest.raises(EnvelopeError):
        open_secret(
            keyring, sealed,
            tenant_id=TENANT, secret_id=uuid.uuid4(), purpose="camera.rtsp",
        )


def test_ciphertext_cannot_be_reused_for_another_purpose(keyring):
    """An RTSP password must not be openable where an API token is expected."""
    sealed = seal(
        keyring, PASSWORD, tenant_id=TENANT, secret_id=SECRET_ID, purpose="camera.rtsp"
    )

    with pytest.raises(EnvelopeError):
        open_secret(
            keyring, sealed,
            tenant_id=TENANT, secret_id=SECRET_ID, purpose="api.token",
        )


def test_platform_secret_is_not_openable_as_a_tenant_secret(keyring):
    sealed = seal(
        keyring, PASSWORD, tenant_id=None, secret_id=SECRET_ID, purpose="gateway.key"
    )

    assert open_secret(
        keyring, sealed, tenant_id=None, secret_id=SECRET_ID, purpose="gateway.key"
    ).decode() == PASSWORD

    with pytest.raises(EnvelopeError):
        open_secret(
            keyring, sealed, tenant_id=TENANT, secret_id=SECRET_ID, purpose="gateway.key"
        )


# --- Tampering ------------------------------------------------------------------------

def test_flipping_a_ciphertext_bit_is_detected(keyring):
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(keyring, PASSWORD, **ctx)
    corrupted = bytearray(sealed.ciphertext)
    corrupted[0] ^= 0x01

    with pytest.raises(EnvelopeError):
        open_secret(
            keyring,
            SealedSecret(
                sealed.kek_id, sealed.wrapped_dek, sealed.dek_nonce,
                bytes(corrupted), sealed.ciphertext_nonce,
            ),
            **ctx,
        )


def test_swapping_in_another_rows_data_key_is_detected(keyring):
    """Mixing one row's wrapped DEK with another's ciphertext must not decrypt."""
    ctx_a = {"tenant_id": TENANT, "secret_id": uuid.uuid4(), "purpose": "camera.rtsp"}
    ctx_b = {"tenant_id": TENANT, "secret_id": uuid.uuid4(), "purpose": "camera.rtsp"}
    a = seal(keyring, PASSWORD, **ctx_a)
    b = seal(keyring, "different", **ctx_b)

    with pytest.raises(EnvelopeError):
        open_secret(
            keyring,
            SealedSecret(
                a.kek_id, b.wrapped_dek, b.dek_nonce, a.ciphertext, a.ciphertext_nonce
            ),
            **ctx_a,
        )


def test_a_different_master_key_cannot_open_it(keyring):
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(keyring, PASSWORD, **ctx)
    attacker = KeyRing({"v1": generate_master_key()}, "v1")

    with pytest.raises(EnvelopeError):
        open_secret(attacker, sealed, **ctx)


def test_missing_key_names_the_id_it_needs(keyring):
    """An operator has to be able to tell *which* key is absent."""
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(keyring, PASSWORD, **ctx)
    other = KeyRing({"v9": generate_master_key()}, "v9")

    with pytest.raises(EnvelopeError, match="v1"):
        open_secret(other, sealed, **ctx)


# --- Serialisation --------------------------------------------------------------------

def test_row_roundtrip(keyring):
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(keyring, PASSWORD, **ctx)
    restored = SealedSecret.from_row(sealed.as_row())

    assert open_secret(keyring, restored, **ctx).decode() == PASSWORD


def test_malformed_row_raises_cleanly(keyring):
    with pytest.raises(EnvelopeError):
        SealedSecret.from_row({"kek_id": "v1"})
    with pytest.raises(EnvelopeError):
        SealedSecret.from_row(
            {"kek_id": "v1", "wrapped_dek": "!!not base64!!", "dek_nonce": "",
             "ciphertext": "", "ciphertext_nonce": ""}
        )


# --- Input limits ---------------------------------------------------------------------

def test_empty_value_is_refused(keyring):
    """An empty credential is a bug upstream, and storing it hides that bug."""
    with pytest.raises(EnvelopeError):
        seal(keyring, "", tenant_id=TENANT, secret_id=SECRET_ID, purpose="camera.rtsp")


def test_oversized_value_is_refused(keyring):
    with pytest.raises(EnvelopeError):
        seal(
            keyring, "x" * 20_000,
            tenant_id=TENANT, secret_id=SECRET_ID, purpose="camera.rtsp",
        )


# --- Key rotation ---------------------------------------------------------------------

def test_rotation_keeps_old_secrets_readable():
    """A rotation that made existing credentials unreadable would take every camera
    offline at once."""
    old = generate_master_key()
    keyring_v1 = KeyRing({"v1": old}, "v1")
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(keyring_v1, PASSWORD, **ctx)

    # v2 arrives; both keys are present.
    keyring_v2 = KeyRing({"v1": old, "v2": generate_master_key()}, "v2")
    assert open_secret(keyring_v2, sealed, **ctx).decode() == PASSWORD

    rewrapped = rewrap(keyring_v2, sealed, **ctx)
    assert rewrapped.kek_id == "v2"
    # The credential itself was never re-encrypted, only its data key.
    assert rewrapped.ciphertext == sealed.ciphertext
    assert open_secret(keyring_v2, rewrapped, **ctx).decode() == PASSWORD


def test_rewrap_is_idempotent(keyring):
    """A rotation pass interrupted halfway must be safe to re-run."""
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(keyring, PASSWORD, **ctx)

    assert rewrap(keyring, sealed, **ctx) is sealed


def test_rewrapped_secret_is_unreadable_without_the_new_key():
    old = generate_master_key()
    new = generate_master_key()
    ctx = {"tenant_id": TENANT, "secret_id": SECRET_ID, "purpose": "camera.rtsp"}
    sealed = seal(KeyRing({"v1": old}, "v1"), PASSWORD, **ctx)
    rewrapped = rewrap(KeyRing({"v1": old, "v2": new}, "v2"), sealed, **ctx)

    with pytest.raises(EnvelopeError):
        open_secret(KeyRing({"v1": old}, "v1"), rewrapped, **ctx)


# --- Key loading ----------------------------------------------------------------------

def test_keyring_loads_raw_and_base64_keys(tmp_path: Path):
    (tmp_path / "v1.key").write_bytes(generate_master_key())
    (tmp_path / "v2.key").write_bytes(base64.b64encode(generate_master_key()))
    if os.name == "posix":
        for entry in tmp_path.glob("*.key"):
            entry.chmod(0o600)

    keyring = KeyRing.from_directory(tmp_path)

    assert keyring.key_ids == ["v1", "v2"]
    # The newest key becomes active by default, so adding a file is enough to rotate.
    assert keyring.active_id == "v2"


def test_wrong_length_key_is_rejected(tmp_path: Path):
    (tmp_path / "v1.key").write_bytes(b"too-short")
    if os.name == "posix":
        (tmp_path / "v1.key").chmod(0o600)

    with pytest.raises(EnvelopeError, match="bytes"):
        KeyRing.from_directory(tmp_path)


def test_empty_directory_is_rejected(tmp_path: Path):
    with pytest.raises(EnvelopeError, match="No \\*.key"):
        KeyRing.from_directory(tmp_path)


def test_missing_directory_is_rejected(tmp_path: Path):
    with pytest.raises(EnvelopeError, match="does not exist"):
        KeyRing.from_directory(tmp_path / "nope")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_world_writable_key_is_refused(tmp_path: Path):
    """Anyone who can rewrite the master key can substitute one they control, then wait
    for secrets to be re-sealed under it. No deployment makes that acceptable."""
    key = tmp_path / "v1.key"
    key.write_bytes(generate_master_key())
    key.chmod(0o666)

    with pytest.raises(EnvelopeError, match="writable"):
        KeyRing.from_directory(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_writable_key_loads_when_strictness_is_off(tmp_path: Path, caplog):
    """Docker Desktop on Windows reports every bind mount as 0777, so the check would fire
    on every developer machine. Relaxing it stays loud."""
    key = tmp_path / "v1.key"
    key.write_bytes(generate_master_key())
    key.chmod(0o666)

    with caplog.at_level("WARNING"):
        keyring = KeyRing.from_directory(tmp_path, strict_permissions=False)

    assert keyring.key_ids == ["v1"]
    assert "master_key_writable_permissions_not_enforced" in caplog.text


def test_strictness_follows_the_environment(tmp_path: Path):
    """Production must not be able to inherit a developer's relaxed setting."""
    from types import SimpleNamespace

    from csense_shared.security.envelope import keyring_from_settings

    (tmp_path / "v1.key").write_bytes(generate_master_key())
    if os.name == "posix":
        (tmp_path / "v1.key").chmod(0o400)

    local = SimpleNamespace(
        environment="local", master_key_dir=str(tmp_path), master_key_active_id=""
    )
    production = SimpleNamespace(
        environment="production", master_key_dir=str(tmp_path), master_key_active_id=""
    )

    # Both load a correctly-permissioned key; the difference is only in what they tolerate.
    assert keyring_from_settings(local).active_id == "v1"
    assert keyring_from_settings(production).active_id == "v1"

    if os.name == "posix":
        (tmp_path / "v1.key").chmod(0o666)
        assert keyring_from_settings(local).active_id == "v1"
        with pytest.raises(EnvelopeError, match="writable"):
            keyring_from_settings(production)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_world_readable_key_warns_but_loads(tmp_path: Path, caplog):
    """Docker mounts compose secrets 0444 so an unprivileged container user can read them.
    Refusing would make the correct containerised setup impossible and teach people to
    turn the check off."""
    key = tmp_path / "v1.key"
    key.write_bytes(generate_master_key())
    key.chmod(0o444)

    with caplog.at_level("WARNING"):
        keyring = KeyRing.from_directory(tmp_path)

    assert keyring.key_ids == ["v1"]
    assert "master_key_broadly_readable" in caplog.text


def test_active_key_must_exist():
    with pytest.raises(EnvelopeError, match="not among"):
        KeyRing({"v1": generate_master_key()}, "v2")


def test_generated_key_is_the_right_size():
    assert len(generate_master_key()) == KEY_BYTES
    assert generate_master_key() != generate_master_key()
