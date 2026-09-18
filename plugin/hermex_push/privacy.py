"""Sealed previews and keyed identifiers. Nothing here ever returns plaintext to a caller that
could forward it to the relay: ``seal`` returns ``None`` on any failure and the payload builder
then sends a content-free event.

Wire format of ``sealed``: base64 of ``nonce(12) || AES-256-GCM ciphertext || tag(16)`` over the
UTF-8 JSON of the preview, with AAD ``hermex-preview-v1:<sha256 hex of install_key>``.
Identifiers are HMAC-SHA256 truncated to 32 hex chars, keyed by a value derived from the install
key, so the relay can dedupe and group without learning session ids' relationship to content.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from typing import Any, Optional

logger = logging.getLogger("hermex_push")

AAD_PREFIX = b"hermex-preview-v1:"
NONCE_BYTES = 12
ID_HEX_CHARS = 32


def preview_aad(install_key: str) -> bytes:
    return AAD_PREFIX + hashlib.sha256(install_key.encode("utf-8")).hexdigest().encode("ascii")


def seal(preview: dict[str, Any], *, preview_key: bytes, install_key: str) -> Optional[str]:
    """Encrypt ``preview``; ``None`` (never plaintext) if anything goes wrong."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(NONCE_BYTES)
        plaintext = json.dumps(preview, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(preview_key).encrypt(nonce, plaintext, preview_aad(install_key))
        return base64.b64encode(nonce + ciphertext).decode("ascii")
    except Exception:
        # Deliberately no preview content in the log line.
        logger.warning("hermex-push: sealing a preview failed; sending a content-free notification", exc_info=True)
        return None


def unseal(sealed: str, *, preview_key: bytes, install_key: str) -> dict[str, Any]:
    """Inverse of :func:`seal`; used by tests and mirrors what the phone does."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(sealed)
    nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    return json.loads(AESGCM(preview_key).decrypt(nonce, ciphertext, preview_aad(install_key)))


def _id_key(install_key: str) -> bytes:
    return hashlib.sha256(b"hermex-ids-v1:" + install_key.encode("utf-8")).digest()


def keyed_id(label: str, value: str, *, install_key: str) -> str:
    """Deterministic opaque id for ``value`` under ``label`` (``event``, ``thread``, ``collapse``)."""
    mac = hmac.new(_id_key(install_key), f"{label}:{value}".encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:ID_HEX_CHARS]
