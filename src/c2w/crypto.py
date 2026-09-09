"""Envelope encryption for per-connection credentials.

CommPeak S3 tokens, CommPeak CDR API keys and Wasabi keys are stored in
PostgreSQL, never in plaintext.  Each value is sealed with AES-256-GCM under a
per-brand data key, which is itself sealed under the process master key
(``C2W_MASTER_KEY``, delivered by systemd ``LoadCredential=``).

Rotating a brand's data key therefore re-seals only that brand's rows, and the
master key never leaves the unit's credential directory.

Ciphertext layout (then urlsafe-base64 encoded)::

    b"c2w1" | key_id (16 bytes) | nonce (12 bytes) | ciphertext+tag
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from c2w.config import get_bootstrap

_MAGIC: Final[bytes] = b"c2w1"
_KEY_ID_LEN: Final[int] = 16
_NONCE_LEN: Final[int] = 12
_KEY_LEN: Final[int] = 32


class CryptoError(RuntimeError):
    """Raised when sealing or unsealing fails."""


@dataclass(frozen=True, slots=True)
class SealedValue:
    """A sealed credential plus the id of the data key that sealed it."""

    ciphertext: str
    key_id: str


def _load_master_key() -> bytes:
    raw = get_bootstrap().master_key.get_secret_value()
    if not raw:
        raise CryptoError(
            "C2W_MASTER_KEY (or C2W_MASTER_KEY_FILE) is not set. Generate one with "
            "`python -c 'import os,base64;"
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())'` "
            "and deliver it via systemd LoadCredential."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise CryptoError("C2W_MASTER_KEY is not valid urlsafe base64") from exc
    if len(key) != _KEY_LEN:
        raise CryptoError(f"C2W_MASTER_KEY must decode to {_KEY_LEN} bytes, got {len(key)}")
    return key


def generate_data_key() -> tuple[str, str]:
    """Create a new per-brand data key.

    Returns ``(key_id, wrapped_key)``.  Persist both on the brand row; the raw
    key material is never returned.
    """
    key_id = os.urandom(_KEY_ID_LEN).hex()
    data_key = os.urandom(_KEY_LEN)
    wrapped = _seal_with(_load_master_key(), data_key, aad=key_id.encode())
    return key_id, wrapped


def _unwrap_data_key(key_id: str, wrapped_key: str) -> bytes:
    return _open_with(_load_master_key(), wrapped_key, aad=key_id.encode())


def seal(plaintext: str, *, key_id: str, wrapped_key: str, aad: str = "") -> str:
    """Seal a credential under a brand data key."""
    if not plaintext:
        return ""
    data_key = _unwrap_data_key(key_id, wrapped_key)
    return _seal_with(data_key, plaintext.encode(), aad=aad.encode(), key_id=key_id)


def open_sealed(ciphertext: str, *, key_id: str, wrapped_key: str, aad: str = "") -> str:
    """Unseal a credential.  Returns ``""`` for an empty stored value."""
    if not ciphertext:
        return ""
    data_key = _unwrap_data_key(key_id, wrapped_key)
    return _open_with(data_key, ciphertext, aad=aad.encode()).decode()


def _seal_with(key: bytes, plaintext: bytes, *, aad: bytes = b"", key_id: str = "") -> str:
    nonce = os.urandom(_NONCE_LEN)
    body = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    kid = bytes.fromhex(key_id) if key_id else b"\x00" * _KEY_ID_LEN
    return base64.urlsafe_b64encode(_MAGIC + kid + nonce + body).decode()


def _open_with(key: bytes, blob: str, *, aad: bytes = b"") -> bytes:
    try:
        raw = base64.urlsafe_b64decode(blob)
    except Exception as exc:
        raise CryptoError("sealed value is not valid base64") from exc
    head = len(_MAGIC) + _KEY_ID_LEN
    if len(raw) < head + _NONCE_LEN + 16 or raw[: len(_MAGIC)] != _MAGIC:
        raise CryptoError("sealed value has an unrecognised format")
    nonce = raw[head : head + _NONCE_LEN]
    try:
        return AESGCM(key).decrypt(nonce, raw[head + _NONCE_LEN :], aad or None)
    except InvalidTag as exc:
        raise CryptoError(
            "failed to unseal credential: wrong key, or the associated data does not match"
        ) from exc


def seal_global(plaintext: str, *, aad: str = "") -> str:
    """Seal a value directly under the master key.

    Used for platform-wide secrets that belong to no single brand -- the
    settings table's integration tokens, for instance.  Brand-owned credentials
    go through :func:`seal` instead, so that rotating one company's data key
    never touches another's rows.
    """
    if not plaintext:
        return ""
    return _seal_with(_load_master_key(), plaintext.encode(), aad=aad.encode())


def open_global(ciphertext: str, *, aad: str = "") -> str:
    """Unseal a value sealed by :func:`seal_global`."""
    if not ciphertext:
        return ""
    return _open_with(_load_master_key(), ciphertext, aad=aad.encode()).decode()
