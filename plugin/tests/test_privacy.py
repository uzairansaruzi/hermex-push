from cryptography.exceptions import InvalidTag
import pytest

from hermex_push.privacy import keyed_id, preview_aad, seal, unseal


def test_seal_round_trips_with_matching_install_key(keys):
    sealed = seal({"title": "Hi", "body": "there"}, preview_key=keys.preview_key, install_key=keys.install_key)
    assert unseal(sealed, preview_key=keys.preview_key, install_key=keys.install_key) == {"title": "Hi", "body": "there"}


def test_aad_binds_the_install_key(keys):
    sealed = seal({"body": "x"}, preview_key=keys.preview_key, install_key=keys.install_key)
    with pytest.raises(InvalidTag):
        unseal(sealed, preview_key=keys.preview_key, install_key="cd" * 32)
    assert preview_aad(keys.install_key).startswith(b"hermex-preview-v1:")


def test_ciphertext_never_contains_plaintext(keys):
    sealed = seal({"body": "needle-in-preview"}, preview_key=keys.preview_key, install_key=keys.install_key)
    assert "needle" not in sealed


def test_seal_failure_returns_none_not_plaintext(keys):
    assert seal({"body": object()}, preview_key=keys.preview_key, install_key=keys.install_key) is None
    assert seal({"body": "x"}, preview_key=b"short", install_key=keys.install_key) is None


def test_keyed_ids_are_stable_and_label_scoped(keys):
    a = keyed_id("thread", "sess-1", install_key=keys.install_key)
    assert a == keyed_id("thread", "sess-1", install_key=keys.install_key)
    assert a != keyed_id("collapse", "sess-1", install_key=keys.install_key)
    assert a != keyed_id("thread", "sess-1", install_key="cd" * 32)
    assert len(a) == 32 and "sess" not in a
