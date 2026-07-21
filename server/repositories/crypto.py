"""Fernet encryption for credentials held at rest.

Composio holds the Gmail OAuth refresh tokens; we hold the ``connection_id``
that redeems them. That makes it a bearer credential, so it is encrypted in
``gmail_connections.connection_id_encrypted``.

``gmail_connections.key_version`` already exists, so rotation is
re-encrypt-in-place: bump ``CURRENT_KEY_VERSION``, add the new key to
``OPENPOKE_DATA_KEYS``, re-encrypt rows whose ``key_version`` is behind. No
migration.

Key material comes from the environment (local) or a secrets manager injected as
env (cloud). It is deliberately *not* a ``Settings`` field: ``server/config.py``
belongs to Phase 0 and secrets should not sit in an object that gets logged or
serialised.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from ..logging_config import logger

#: ``OPENPOKE_DATA_KEY`` holds the current key. ``OPENPOKE_DATA_KEY_V<n>`` holds
#: retired keys so rows encrypted under them can still be read during rotation.
_CURRENT_KEY_ENV = "OPENPOKE_DATA_KEY"
CURRENT_KEY_VERSION = 1


class DataKeyMissing(RuntimeError):
    """Raised when encryption is requested but no data key is configured."""


def _key_for(version: int) -> str | None:
    if version == CURRENT_KEY_VERSION:
        return os.getenv(_CURRENT_KEY_ENV) or None
    return os.getenv(f"{_CURRENT_KEY_ENV}_V{version}") or None


def is_configured() -> bool:
    """True when a current data key is present."""
    return bool(_key_for(CURRENT_KEY_VERSION))


def _fernet(version: int) -> Fernet:
    key = _key_for(version)
    if not key:
        raise DataKeyMissing(
            f"{_CURRENT_KEY_ENV} is not set; generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'`"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> tuple[str, int]:
    """Return ``(ciphertext, key_version)``. Raises ``DataKeyMissing`` if unset."""
    token = _fernet(CURRENT_KEY_VERSION).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii"), CURRENT_KEY_VERSION


def decrypt(ciphertext: str, key_version: int = CURRENT_KEY_VERSION) -> str | None:
    """Decrypt, returning ``None`` when the key is missing or the token is bad.

    Never logs the ciphertext or the plaintext — only the key version.
    """
    try:
        return _fernet(key_version).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except DataKeyMissing:
        logger.warning("cannot decrypt stored credential", extra={"key_version": key_version})
        return None
    except (InvalidToken, ValueError):
        logger.warning("stored credential failed to decrypt", extra={"key_version": key_version})
        return None


__all__ = [
    "CURRENT_KEY_VERSION",
    "DataKeyMissing",
    "decrypt",
    "encrypt",
    "is_configured",
]
