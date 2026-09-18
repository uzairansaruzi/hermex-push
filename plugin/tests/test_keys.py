import stat

from hermex_push.keys import load_or_create_keys


def test_keys_are_created_once_with_private_permissions(hermes_home):
    first = load_or_create_keys()
    second = load_or_create_keys()
    assert first == second
    assert len(first.install_key) == 64 and int(first.install_key, 16)
    assert len(first.preview_key) == 32
    directory = hermes_home / "plugins" / "hermex-push"
    for name in ("install_key", "preview_key"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
