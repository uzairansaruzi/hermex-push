"""Install and preview keys, created once and never rotated behind a paired device.

Both live in ``<hermes root>/plugins/hermex-push/`` (the path pinned in hermex#490), mode 0600,
created under a file lock so two gateway processes starting together agree on one key.

- ``install_key``: 64 hex chars. The relay capability; the phone sends it on every relay call.
- ``preview_key``: 32 raw bytes. AES-256-GCM key for sealed previews; the phone's Notification
  Service Extension holds a copy and the relay never sees it.
"""

from __future__ import annotations

import base64
import fcntl
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from . import PLUGIN_NAME

PREVIEW_KEY_BYTES = 32
INSTALL_KEY_HEX_CHARS = 64


def hermes_root() -> Path:
    """The root Hermes home, never a profile home. Sessions run under a profile scope
    (``HERMES_HOME=<root>/profiles/<name>``) and each profile installs its own copy of the plugin,
    but one host pairs with one phone, so every profile must share the same keys."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        return home.parents[1] if home.parent.name == "profiles" else home


def key_dir(home: Path | None = None) -> Path:
    return (home or hermes_root()) / "plugins" / PLUGIN_NAME


@dataclass(frozen=True)
class Keys:
    install_key: str
    preview_key: bytes

    @property
    def preview_key_b64(self) -> str:
        return base64.b64encode(self.preview_key).decode("ascii")


def _write_private(path: Path, data: bytes) -> None:
    """Atomic 0600 write: tmp file with the final mode, then rename over the target."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _read_or_create(path: Path, make: "callable[[], bytes]") -> bytes:
    if path.exists():
        os.chmod(path, 0o600)
        return path.read_bytes()
    data = make()
    _write_private(path, data)
    return data


def load_or_create_keys(home: Path | None = None) -> Keys:
    """Return the host's keys, generating any that are missing. Safe to call from every process."""
    directory = key_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".keys.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            install = _read_or_create(
                directory / "install_key", lambda: secrets.token_hex(INSTALL_KEY_HEX_CHARS // 2).encode("ascii")
            ).decode("ascii").strip()
            preview = _read_or_create(directory / "preview_key", lambda: secrets.token_bytes(PREVIEW_KEY_BYTES))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    if len(install) != INSTALL_KEY_HEX_CHARS or len(preview) != PREVIEW_KEY_BYTES:
        raise ValueError("hermex-push key files are malformed; remove them to regenerate (this unpairs every device)")
    return Keys(install_key=install, preview_key=preview)
