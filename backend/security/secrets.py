from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedSecret:
    """Serialized AES-GCM secret components safe to persist in a database."""

    ciphertext: str
    nonce: str
    key_version: int = 1


def _derive_key(master_key: str) -> bytes:
    if not master_key or not master_key.strip():
        raise ValueError("provider encryption key is required")
    return hashlib.sha256(master_key.encode("utf-8")).digest()


def encrypt_secret(secret: str, *, master_key: str, associated_data: str) -> EncryptedSecret:
    if not secret:
        raise ValueError("secret must not be empty")
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_derive_key(master_key)).encrypt(
        nonce,
        secret.encode("utf-8"),
        associated_data.encode("utf-8"),
    )
    return EncryptedSecret(
        ciphertext=base64.urlsafe_b64encode(encrypted).decode("ascii"),
        nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
    )


def decrypt_secret(value: EncryptedSecret, *, master_key: str, associated_data: str) -> str:
    try:
        encrypted = base64.urlsafe_b64decode(value.ciphertext.encode("ascii"))
        nonce = base64.urlsafe_b64decode(value.nonce.encode("ascii"))
        plain = AESGCM(_derive_key(master_key)).decrypt(
            nonce,
            encrypted,
            associated_data.encode("utf-8"),
        )
    except (InvalidTag, ValueError, TypeError) as exc:
        raise ValueError("unable to decrypt provider secret") from exc
    return plain.decode("utf-8")
