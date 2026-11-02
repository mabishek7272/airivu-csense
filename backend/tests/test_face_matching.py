import numpy as np
import pytest

from csense_shared.faces import matching as m


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=m.EMBEDDING_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _entry(name, seed, list_type="known"):
    return m.GalleryEntry(f"id-{name}", name, list_type, _unit(seed))


def test_template_roundtrip_is_lossless_and_unit_length():
    v = _unit(1) * 7.5  # un-normalised input, as InsightFaceEngine.embed returns
    decoded = m.decode_template(m.encode_template(v))
    assert decoded.shape == (m.EMBEDDING_DIM,)
    assert abs(float(np.linalg.norm(decoded)) - 1.0) < 1e-5
    assert float(decoded @ _unit(1)) > 0.9999


def test_encoded_template_fits_the_secret_envelope_cap():
    assert len(m.encode_template(_unit(2))) < 8 * 1024


@pytest.mark.parametrize("bad", [np.zeros(m.EMBEDDING_DIM), np.ones(10), np.full(m.EMBEDDING_DIM, np.nan)])
def test_bad_vectors_are_rejected(bad):
    with pytest.raises(m.TemplateError):
        m.normalise(bad)


def test_truncated_stored_template_is_rejected():
    with pytest.raises(m.TemplateError):
        m.decode_template("AAAA")


def test_known_person_matches_by_name():
    gallery = [_entry("sara", 1), _entry("abel", 2)]
    cls, attrs = m.classify(_unit(1), gallery)
    assert cls == m.CLASS_KNOWN
    assert attrs["name"] == "sara" and attrs["similarity"] > 0.99


def test_watchlist_person_gets_watchlist_class():
    cls, attrs = m.classify(_unit(3), [_entry("banned", 3, "watchlist"), _entry("sara", 1)])
    assert cls == m.CLASS_WATCHLIST and attrs["name"] == "banned"


def test_stranger_is_unknown():
    cls, attrs = m.classify(_unit(99), [_entry("sara", 1), _entry("abel", 2)])
    assert cls == m.CLASS_UNKNOWN and attrs is None


def test_empty_gallery_means_everyone_is_unknown():
    assert m.classify(_unit(5), [])[0] == m.CLASS_UNKNOWN


def test_threshold_is_respected():
    a, b = _unit(1), _unit(2)
    mixed = m.normalise(a + 0.6 * b)
    sim = float(mixed @ a)
    assert m.best_match(mixed, [_entry("sara", 1)], threshold=sim - 0.01) is not None
    assert m.best_match(mixed, [_entry("sara", 1)], threshold=sim + 0.01) is None


def test_best_of_multiple_templates_wins_for_one_identity():
    near = m.GalleryEntry("id-x", "x", "known", m.normalise(_unit(1) + 0.1 * _unit(9)))
    far = m.GalleryEntry("id-x", "x", "known", _unit(7))
    assert m.best_match(_unit(1), [far, near]).identity_id == "id-x"
