from __future__ import annotations

import base64
import binascii
import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag


class ModelSecretError(RuntimeError):
    """Fixed-message exception safe for logs and API responses."""

    def __init__(self) -> None:
        super().__init__("model credential is unavailable")


def _master_key() -> bytes:
    raw = os.environ.get("EXTRACTION_MODEL_MASTER_KEY", "")
    try:
        value = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ModelSecretError() from None
    if len(value) != 32 or base64.b64encode(value).decode("ascii") != raw:
        raise ModelSecretError()
    return value


def _aad(config_id: UUID, provider: str, version: int, key_id: str) -> bytes:
    if version != 1 or not provider or not key_id:
        raise ModelSecretError()
    return f"model-config\0{config_id}\0{provider}\0v{version}\0{key_id}".encode()


def encrypt_secret(secret: str, *, config_id: UUID, provider: str, key_id: str = "primary") -> bytes:
    if not secret or "\x00" in secret:
        raise ModelSecretError()
    nonce = os.urandom(12)
    encrypted = AESGCM(_master_key()).encrypt(nonce, secret.encode(), _aad(config_id, provider, 1, key_id))
    return b"v1:" + key_id.encode("ascii") + b":" + base64.b64encode(nonce + encrypted)


def decrypt_secret(ciphertext: bytes, *, config_id: UUID, provider: str) -> str:
    try:
        marker, raw_key_id, payload = ciphertext.split(b":", 2)
        if marker != b"v1":
            raise ValueError
        key_id = raw_key_id.decode("ascii")
        decoded = base64.b64decode(payload, validate=True)
        if len(decoded) < 29:
            raise ValueError
        value = AESGCM(_master_key()).decrypt(decoded[:12], decoded[12:], _aad(config_id, provider, 1, key_id)).decode()
        if not value:
            raise ValueError
        return value
    except ModelSecretError:
        raise
    except (ValueError, UnicodeError, binascii.Error, InvalidTag):
        raise ModelSecretError() from None
